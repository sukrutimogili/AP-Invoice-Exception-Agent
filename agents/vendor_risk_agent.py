"""
agents/vendor_risk_agent.py — LLM-powered vendor risk assessment agent.

For each vendor in the master table the agent gathers four signals:

  1. Auto-creation flag — was this vendor created automatically from a PO/contract
     upload rather than manually onboarded?  (VENDOR_AUTO_CREATED audit event)
  2. Exception history — all exceptions from services/exception_store.py where
     any invoice's vendor name matches this vendor (correlated via audit trail).
  3. Resolution pattern — of those exceptions, how many were approved-override vs.
     rejected vs. still open.
  4. Price-variance trend — PRICE_VARIANCE supporting_data extracted from
     consecutive exceptions to detect systematic price creep.

All signals are assembled into a plain-text context block, rendered into the
prompt at agents/prompts/assess_vendor_risk.md, and sent to the LLM (same
OpenRouter fallback chain as the extraction pipeline).

The LLM is instructed to return {"skip": true} for vendors with nothing notable,
so the returned list contains only actionable flags.  For each flagged vendor a
VENDOR_RISK_FLAGGED audit event is written with the full VendorRiskFlag payload.

Design principles (same as exception_triage_agent.py):
  - Agent boundary at assessment, not action.  No automated approval/rejection.
  - Reuses existing OpenRouterClient — zero new LLM wiring.
  - Fails closed on parse errors: VendorRiskError raised, not silent bad output.
  - assess_vendor_risk() is idempotent — safe to call on a schedule.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

import audit.writer as audit_writer
from audit.writer import _append, _payload
from app.config import get_settings
from extraction.llm_client import LLMCallError, LLMClient, OpenRouterClient
from models.audit_event import AuditEventCreate
from models.enums import AuditEventType, ExceptionReasonCode, HumanAction
from repositories.vendor_repo import list_vendors
from services.exception_store import list_exceptions

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "assess_vendor_risk.md"


# ---------------------------------------------------------------------------
# Public result model
# ---------------------------------------------------------------------------


class VendorRiskFlag(BaseModel):
    """
    Risk assessment result for a single vendor.

    Only vendors with a concrete, data-backed signal are returned — the LLM
    is instructed to return {"skip": true} for vendors with nothing notable.
    """

    vendor_code: str = Field(description="Vendor code from the vendor master.")
    risk_level: Literal["high", "medium", "low"] = Field(
        description="Assessed risk level based on patterns in the data."
    )
    reasons: list[str] = Field(
        min_length=1,
        description="Concrete, data-cited reasons for the flag.",
    )
    recommended_action: str = Field(
        description="Human-reviewable action — never an automated decision.",
    )


class VendorRiskError(Exception):
    """Raised when vendor risk assessment fails (parse or validation error)."""


# ---------------------------------------------------------------------------
# Prompt loading and rendering
# ---------------------------------------------------------------------------


def _load_prompt_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _render_prompt(
    vendor_code: str,
    vendor_name: str,
    is_active: bool,
    auto_creation_info: str,
    exception_count: int,
    exception_details: str,
    resolution_summary: str,
    price_variance_trend: str,
) -> str:
    template = _load_prompt_template()
    template = template.replace("{{ vendor_code }}", vendor_code)
    template = template.replace("{{ vendor_name }}", vendor_name)
    template = template.replace("{{ is_active }}", str(is_active))
    template = template.replace("{{ auto_creation_info }}", auto_creation_info)
    template = template.replace("{{ exception_count }}", str(exception_count))
    template = template.replace("{{ exception_details }}", exception_details)
    template = template.replace("{{ resolution_summary }}", resolution_summary)
    template = template.replace("{{ price_variance_trend }}", price_variance_trend)
    return template


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


def _find_auto_creation_event(
    vendor_code: str,
    all_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the VENDOR_AUTO_CREATED audit event for this vendor_code, or None."""
    for event in all_events:
        if event.get("event_type") != AuditEventType.VENDOR_AUTO_CREATED.value:
            continue
        raw = event.get("payload_json")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if payload.get("vendor_code") == vendor_code:
            return event
    return None


def _build_auto_creation_info(
    vendor_code: str,
    all_events: list[dict[str, Any]],
) -> str:
    event = _find_auto_creation_event(vendor_code, all_events)
    if event is None:
        return "This vendor was NOT auto-created — it was manually onboarded."

    ts = str(event.get("created_at", ""))[:19]
    try:
        payload = json.loads(event.get("payload_json") or "{}")
    except Exception:
        payload = {}

    source = payload.get("source_document", "unknown")
    return (
        f"This vendor WAS auto-created on {ts} from a {source} document upload.  "
        f"It was not manually onboarded through normal vendor registration."
    )


def _get_vendor_invoice_ids(
    vendor_name: str,
    all_events: list[dict[str, Any]],
) -> set[str]:
    """Return all invoice_ids whose audit trail contains this vendor name (case-insensitive)."""
    return {
        e["invoice_id"]
        for e in all_events
        if (e.get("vendor_name") or "").lower() == vendor_name.lower()
        and e.get("invoice_id")
    }


def _build_exception_context(
    vendor_name: str,
    all_events: list[dict[str, Any]],
) -> tuple[int, str, str, str]:
    """
    Build the three exception context strings for the prompt.

    Returns:
        (exception_count, exception_details, resolution_summary, price_variance_trend)
    """
    vendor_invoice_ids = _get_vendor_invoice_ids(vendor_name, all_events)
    all_exceptions = list_exceptions()

    vendor_exceptions = [
        exc for exc in all_exceptions
        if exc.invoice_id in vendor_invoice_ids
    ]

    if not vendor_exceptions:
        return (
            0,
            "No exceptions recorded for this vendor.",
            "No resolution history.",
            "No price variance data.",
        )

    # --- exception_details ---
    detail_lines: list[str] = []
    for exc in vendor_exceptions:
        status = exc.exception_record.status.value
        action = exc.exception_record.human_action
        action_str = action.value if action else "OPEN (unresolved)"
        codes = [r.reason_code.value for r in exc.exception_record.reasons]
        detail_lines.append(
            f"  - Invoice {exc.invoice_id}: reason_codes={codes}, "
            f"status={status}, action={action_str}"
        )
        for reason in exc.exception_record.reasons:
            if reason.supporting_data:
                detail_lines.append(
                    f"    supporting_data: {json.dumps(reason.supporting_data, default=str)}"
                )

    exception_details = "\n".join(detail_lines)

    # --- resolution_summary ---
    approve_count = sum(
        1 for exc in vendor_exceptions
        if exc.exception_record.human_action == HumanAction.APPROVE_OVERRIDE
    )
    reject_count = sum(
        1 for exc in vendor_exceptions
        if exc.exception_record.human_action == HumanAction.REJECT
    )
    open_count = sum(
        1 for exc in vendor_exceptions
        if exc.exception_record.human_action is None
    )
    total = len(vendor_exceptions)

    resolution_summary = (
        f"Out of {total} total exception(s): "
        f"{approve_count} approved with override, "
        f"{reject_count} rejected, "
        f"{open_count} still open/unresolved."
    )

    if approve_count == total and total >= 3:
        resolution_summary += (
            f"  WARNING: ALL {total} exceptions were approved with override — "
            f"no rejections on record, which may indicate rubber-stamp approval."
        )

    # --- price_variance_trend ---
    pv_deltas: list[str] = []
    for exc in vendor_exceptions:
        for reason in exc.exception_record.reasons:
            if reason.reason_code != ExceptionReasonCode.PRICE_VARIANCE:
                continue
            line_variances = reason.supporting_data.get("line_variances", [])
            for lv in line_variances:
                pct = lv.get("price_variance_pct")
                line_num = lv.get("line_number")
                billed = lv.get("billed_unit_price")
                contract = lv.get("contract_unit_price")
                pv_deltas.append(
                    f"  - Invoice {exc.invoice_id}, line {line_num}: "
                    f"billed={billed}, contract={contract}, "
                    f"variance_pct={pct}"
                )

    if not pv_deltas:
        price_variance_trend = "No PRICE_VARIANCE data available for this vendor."
    else:
        price_variance_trend = (
            f"{len(pv_deltas)} PRICE_VARIANCE line(s) recorded:\n"
            + "\n".join(pv_deltas)
        )

    return len(vendor_exceptions), exception_details, resolution_summary, price_variance_trend


# ---------------------------------------------------------------------------
# LLM response parser
# ---------------------------------------------------------------------------


def _parse_llm_response(raw: str, vendor_code: str) -> VendorRiskFlag | None:
    """
    Parse the LLM response.

    Returns:
        VendorRiskFlag — if the LLM flagged this vendor.
        None           — if the LLM returned {"skip": true}.

    Raises:
        VendorRiskError — on parse failure or Pydantic validation failure.
    """
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise VendorRiskError(
            f"LLM returned non-JSON for vendor {vendor_code!r}: {exc}. "
            f"Snippet: {raw[:300]}"
        ) from exc

    if data.get("skip") is True:
        logger.debug("LLM skipped vendor %r — no notable signals", vendor_code)
        return None

    try:
        flag = VendorRiskFlag.model_validate(data)
    except ValidationError as exc:
        raise VendorRiskError(
            f"LLM response failed Pydantic validation for vendor {vendor_code!r}: {exc}. "
            f"Data: {data}"
        ) from exc

    return flag


# ---------------------------------------------------------------------------
# Audit write for VENDOR_RISK_FLAGGED
# ---------------------------------------------------------------------------


def _write_vendor_risk_flagged(flag: VendorRiskFlag) -> None:
    """
    Append a VENDOR_RISK_FLAGGED audit event for one flagged vendor.

    Uses a synthetic invoice_id of "vendor:{vendor_code}" because risk flags
    span multiple invoices and are not tied to a single one.
    """
    _append(
        AuditEventCreate(
            invoice_id=f"vendor:{flag.vendor_code}",
            event_type=AuditEventType.VENDOR_RISK_FLAGGED,
            vendor_name=flag.vendor_code,
            payload_json=_payload(
                vendor_code=flag.vendor_code,
                risk_level=flag.risk_level,
                reasons=flag.reasons,
                recommended_action=flag.recommended_action,
            ),
        )
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def assess_vendor_risk(
    session: Session,
    llm_client: LLMClient | None = None,
) -> list[VendorRiskFlag]:
    """
    Assess risk across all vendors in the vendor master table.

    For each vendor:
      1. Check whether it was auto-created (VENDOR_AUTO_CREATED audit event).
      2. Gather all exceptions from the in-process store matching this vendor.
      3. Build resolution pattern (approve_override / reject / open counts).
      4. Extract price-variance trend from PRICE_VARIANCE supporting_data.
      5. Skip vendors with no exceptions AND no auto-creation flag.
      6. Render all context into the assess_vendor_risk.md prompt template.
      7. Call the LLM.  {"skip": true} → discard and continue.
      8. Validate the response into VendorRiskFlag.
      9. Write VENDOR_RISK_FLAGGED audit event for each flagged vendor.

    Args:
        session:    Active SQLAlchemy Session (read-only — list_vendors only).
        llm_client: Optional override for testing.  Defaults to OpenRouterClient.

    Returns:
        List of VendorRiskFlag (only flagged vendors).  May be empty.
    """
    if llm_client is None:
        settings = get_settings()
        llm_client = OpenRouterClient(settings)

    all_events = audit_writer.get_all_events()
    vendors = list_vendors(session)
    flags: list[VendorRiskFlag] = []

    logger.info("Starting vendor risk assessment for %d vendor(s)", len(vendors))

    for vendor in vendors:
        vendor_code = vendor.vendor_code
        vendor_name = vendor.name

        auto_creation_info = _build_auto_creation_info(vendor_code, all_events)
        is_auto_created = "WAS auto-created" in auto_creation_info

        exception_count, exception_details, resolution_summary, price_variance_trend = (
            _build_exception_context(vendor_name, all_events)
        )

        # Skip vendors with nothing to assess
        if exception_count == 0 and not is_auto_created:
            logger.debug(
                "Skipping vendor %r — no exceptions and not auto-created", vendor_code
            )
            continue

        rendered_prompt = _render_prompt(
            vendor_code=vendor_code,
            vendor_name=vendor_name,
            is_active=vendor.is_active,
            auto_creation_info=auto_creation_info,
            exception_count=exception_count,
            exception_details=exception_details,
            resolution_summary=resolution_summary,
            price_variance_trend=price_variance_trend,
        )

        system_prompt = (
            "You are an AP risk analyst. Follow the instructions in the user message exactly. "
            'Return only valid JSON — either a risk flag object or {"skip": true}.'
        )

        try:
            raw_response = llm_client.complete(system_prompt, rendered_prompt)
        except LLMCallError as exc:
            logger.error(
                "LLM call failed for vendor %r — skipping: %s", vendor_code, exc
            )
            continue

        try:
            flag = _parse_llm_response(raw_response, vendor_code)
        except VendorRiskError as exc:
            logger.error(
                "Parse error for vendor %r — skipping: %s", vendor_code, exc
            )
            continue

        if flag is None:
            continue

        _write_vendor_risk_flagged(flag)
        flags.append(flag)
        logger.info("Vendor %r flagged at risk_level=%s", vendor_code, flag.risk_level)

    logger.info(
        "Vendor risk assessment complete: %d/%d vendor(s) flagged",
        len(flags),
        len(vendors),
    )
    return flags
