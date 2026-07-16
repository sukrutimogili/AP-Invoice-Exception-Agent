"""
agents/exception_triage_agent.py — LLM-powered exception investigation agent.

Investigates a raised invoice exception by gathering context from three sources:
  1. The current exception's reason codes + supporting data
     (services/exception_store.py → ExceptionDecision)
  2. The same vendor's prior exception history from the same in-process store
     (pattern detection: e.g. "3 PRICE_VARIANCE exceptions, all approved")
  3. The full audit trail for this invoice
     (audit/writer.py → get_all_events())

All gathered context is sent to the LLM (OpenRouter, same fallback chain as the
extraction pipeline) with the prompt at agents/prompts/investigate_exception.md,
which explicitly instructs the model to:
  - Reason only from the provided data
  - Never invent facts not present in the context
  - Produce a recommendation for a human reviewer, not an automated decision

After the LLM returns, the result is validated with Pydantic and an
INVESTIGATION_COMPLETED audit event is written (models/enums.AuditEventType).

Design principles:
  - Agent boundary at investigation, not action.  The human still clicks
    approve/reject — this agent only surfaces patterns and evidence.
  - Reuses the existing OpenRouterClient and fallback chain (no new LLM wiring).
  - Returns a typed ExceptionInvestigation; callers never touch raw JSON.
  - Fails closed: if the LLM returns an unparseable response, raises
    InvestigationError rather than silently returning a wrong result.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

import audit.writer as audit_writer
from app.config import get_settings
from audit.writer import AuditEventCreate, _append, _payload
from extraction.llm_client import LLMCallError, LLMClient, OpenRouterClient
from models.audit_event import AuditEventCreate
from models.enums import AuditEventType
from services.exception_store import list_exceptions

logger = logging.getLogger(__name__)

# Path to the investigation prompt template
_PROMPT_PATH = Path(__file__).parent / "prompts" / "investigate_exception.md"


# ---------------------------------------------------------------------------
# Public result model
# ---------------------------------------------------------------------------


class ExceptionInvestigation(BaseModel):
    """
    Structured output of the exception investigation agent.

    Carries the LLM's triage recommendation together with the evidence it
    used, ready for display in the UI above the approve/reject controls.
    The human reviewer retains full authority — this is advisory only.
    """

    root_cause_summary: str = Field(
        description="1–3 sentences explaining the most likely root cause, citing data from the context.",
    )
    recommended_action: Literal[
        "APPROVE_OVERRIDE",
        "REJECT",
        "REQUEST_CORRECTED_DOCUMENT",
        "ESCALATE",
    ] = Field(
        description="The agent's recommended action for the human reviewer.",
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="How confident the agent is in its recommendation given the available data.",
    )
    supporting_context: list[str] = Field(
        default_factory=list,
        description="Concrete evidence items that informed the recommendation (vendor history, audit trail patterns, etc.).",
    )


class InvestigationError(Exception):
    """Raised when the investigation cannot be completed (LLM failure or parse error)."""


# ---------------------------------------------------------------------------
# Prompt loading and rendering
# ---------------------------------------------------------------------------


def _load_prompt_template() -> str:
    """Load the investigation prompt from disk."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _render_prompt(
    invoice_id: str,
    exception_reasons_text: str,
    vendor_history_text: str,
    audit_trail_text: str,
) -> str:
    """
    Fill the {{ variable }} placeholders in the prompt template.

    Uses simple string replacement — no templating library dependency.
    """
    template = _load_prompt_template()
    template = template.replace("{{ invoice_id }}", invoice_id)
    template = template.replace("{{ exception_reasons }}", exception_reasons_text)
    template = template.replace("{{ vendor_history }}", vendor_history_text)
    template = template.replace("{{ audit_trail }}", audit_trail_text)
    return template


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


def _build_exception_reasons_text(invoice_id: str) -> tuple[str, str | None]:
    """
    Load the current exception from the store and format its reason codes.

    Returns (formatted_text, vendor_name_from_audit_trail).
    vendor_name is extracted from the audit trail rather than the exception store
    (the store carries only reason codes, not invoice metadata).
    """
    exceptions = list_exceptions()
    target = next((e for e in exceptions if e.invoice_id == invoice_id), None)

    if target is None:
        return f"No exception record found for invoice_id={invoice_id!r}.", None

    lines: list[str] = []
    for reason in target.exception_record.reasons:
        lines.append(f"- Reason code: {reason.reason_code.value}")
        if reason.supporting_data:
            lines.append(f"  Supporting data: {json.dumps(reason.supporting_data, default=str)}")

    return "\n".join(lines) if lines else "(no reason details available)", None


def _extract_vendor_name_from_audit(invoice_id: str, all_events: list[dict[str, Any]]) -> str | None:
    """Pull vendor_name from the first audit event that has one for this invoice."""
    for event in all_events:
        if event.get("invoice_id") == invoice_id and event.get("vendor_name"):
            return event["vendor_name"]
    return None


def _build_vendor_history_text(
    current_invoice_id: str,
    vendor_name: str | None,
    all_events: list[dict[str, Any]],
) -> str:
    """
    Summarise this vendor's prior exceptions from the in-process store.

    Looks at every registered exception whose invoice appears in the audit trail
    with a matching vendor_name, then counts outcomes (approved, rejected, open).
    Returns a plain-text summary suitable for inclusion in the prompt.
    """
    if not vendor_name:
        return "Vendor name not available — cannot determine prior exception history."

    all_exceptions = list_exceptions()

    # Build set of invoice_ids that belong to this vendor (from audit trail)
    vendor_invoice_ids: set[str] = {
        event["invoice_id"]
        for event in all_events
        if (event.get("vendor_name") or "").lower() == vendor_name.lower()
    }

    # Collect prior exceptions (exclude the current invoice)
    prior = [
        exc
        for exc in all_exceptions
        if exc.invoice_id in vendor_invoice_ids and exc.invoice_id != current_invoice_id
    ]

    if not prior:
        return (
            f"No prior exception history found for vendor '{vendor_name}' "
            "in the current session store."
        )

    lines: list[str] = [
        f"Vendor '{vendor_name}' has {len(prior)} prior exception(s) on record:\n"
    ]

    # Group by reason code across all prior exceptions
    reason_counts: dict[str, int] = {}
    reason_outcomes: dict[str, list[str]] = {}  # reason_code → list of human_actions

    for exc in prior:
        status = exc.exception_record.status.value
        human_action = exc.exception_record.human_action
        action_str = human_action.value if human_action else "OPEN (unresolved)"

        for reason in exc.exception_record.reasons:
            rc = reason.reason_code.value
            reason_counts[rc] = reason_counts.get(rc, 0) + 1
            reason_outcomes.setdefault(rc, []).append(action_str)

        lines.append(
            f"  - Invoice {exc.invoice_id}: status={status}, action={action_str}, "
            f"reason_codes={[r.reason_code.value for r in exc.exception_record.reasons]}"
        )

    lines.append("\nReason code summary across all prior exceptions:")
    for rc, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        outcomes = reason_outcomes[rc]
        approve_count = sum(1 for o in outcomes if "APPROVE" in o)
        reject_count = sum(1 for o in outcomes if "REJECT" in o)
        open_count = sum(1 for o in outcomes if "OPEN" in o)
        lines.append(
            f"  - {rc}: {count} occurrence(s) — "
            f"{approve_count} approved, {reject_count} rejected, {open_count} open/unresolved"
        )

    return "\n".join(lines)


def _build_audit_trail_text(invoice_id: str, all_events: list[dict[str, Any]]) -> str:
    """
    Format the full audit trail for this invoice as plain text for the prompt.
    """
    invoice_events = [e for e in all_events if e.get("invoice_id") == invoice_id]

    if not invoice_events:
        return f"No audit events found for invoice_id={invoice_id!r}."

    lines: list[str] = [f"Audit trail for invoice {invoice_id} ({len(invoice_events)} event(s)):\n"]
    for event in invoice_events:
        ts = str(event.get("created_at", ""))[:19]
        etype = event.get("event_type", "UNKNOWN")
        actor = event.get("actor_id") or "system"

        payload_str = ""
        raw_payload = event.get("payload_json")
        if raw_payload:
            try:
                payload_data = json.loads(raw_payload)
                # Condense the payload to key fields only to keep token count manageable
                key_fields = {
                    k: v
                    for k, v in payload_data.items()
                    if k in (
                        "reason_codes", "recommendation", "grand_total", "actor_id",
                        "resolution_notes", "overridden_reason_codes", "rejected_reason_codes",
                        "overall_passed", "discount_pct", "annualized_return",
                        "extraction_status", "document_type", "natural_key",
                    )
                }
                if key_fields:
                    payload_str = f" | {json.dumps(key_fields, default=str)}"
            except Exception:
                pass

        lines.append(f"  [{ts}] {etype} (actor: {actor}){payload_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM response parser
# ---------------------------------------------------------------------------


def _parse_llm_response(raw: str) -> ExceptionInvestigation:
    """
    Parse and validate the LLM's JSON response into ExceptionInvestigation.

    Strips markdown code fences if the model wraps the JSON (defensive parsing,
    same strategy as extraction/parser.py).

    Raises:
        InvestigationError: if the response cannot be parsed or fails validation.
    """
    # Strip markdown fences (```json … ```) if present
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise InvestigationError(
            f"LLM returned non-JSON response: {exc}. "
            f"Raw response snippet: {raw[:300]}"
        ) from exc

    try:
        return ExceptionInvestigation.model_validate(data)
    except ValidationError as exc:
        raise InvestigationError(
            f"LLM response failed Pydantic validation: {exc}. "
            f"Parsed data: {data}"
        ) from exc


# ---------------------------------------------------------------------------
# Audit write for INVESTIGATION_COMPLETED
# ---------------------------------------------------------------------------


def _write_investigation_completed(
    invoice_id: str,
    vendor_name: str | None,
    investigation: ExceptionInvestigation,
) -> None:
    """
    Append an INVESTIGATION_COMPLETED audit event containing the full result.

    Follows the same pattern as all other write_* functions in audit/writer.py.
    Written here rather than audit/writer.py to keep the investigation concerns
    co-located with the agent that produces them.
    """
    _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.INVESTIGATION_COMPLETED,
            vendor_name=vendor_name,
            payload_json=_payload(
                root_cause_summary=investigation.root_cause_summary,
                recommended_action=investigation.recommended_action,
                confidence=investigation.confidence,
                supporting_context=investigation.supporting_context,
            ),
        )
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def investigate_exception(
    invoice_id: str,
    llm_client: LLMClient | None = None,
) -> ExceptionInvestigation:
    """
    Investigate an open invoice exception and return a triage recommendation.

    Steps:
      1. Load the exception's reason codes + supporting data from the store.
      2. Pull the full audit trail for this invoice.
      3. Extract the vendor name from the audit trail (used for vendor history lookup).
      4. Build a summary of the same vendor's prior exceptions (pattern detection).
      5. Render all gathered context into the investigation prompt template.
      6. Call the LLM (OpenRouter fallback chain) with the rendered prompt.
      7. Parse and validate the response into ExceptionInvestigation.
      8. Write INVESTIGATION_COMPLETED audit event with the full result.
      9. Return the typed ExceptionInvestigation.

    Args:
        invoice_id:  The invoice ID whose exception should be investigated.
        llm_client:  Optional LLM client override (injected in tests to avoid
                     real network calls).  Defaults to OpenRouterClient built
                     from the application settings.

    Returns:
        ExceptionInvestigation — the LLM's structured triage recommendation.

    Raises:
        InvestigationError — if the exception is not found, the LLM call fails,
                             or the response cannot be validated.
    """
    logger.info("Starting exception investigation", extra={"invoice_id": invoice_id})

    # -- Step 1: Gather exception reasons --
    exception_reasons_text, _ = _build_exception_reasons_text(invoice_id)

    # -- Step 2: Pull full audit trail --
    all_events = audit_writer.get_all_events()
    audit_trail_text = _build_audit_trail_text(invoice_id, all_events)

    # -- Step 3: Extract vendor name for history lookup --
    vendor_name = _extract_vendor_name_from_audit(invoice_id, all_events)

    # -- Step 4: Build vendor history summary --
    vendor_history_text = _build_vendor_history_text(invoice_id, vendor_name, all_events)

    # -- Step 5: Render the prompt --
    rendered_prompt = _render_prompt(
        invoice_id=invoice_id,
        exception_reasons_text=exception_reasons_text,
        vendor_history_text=vendor_history_text,
        audit_trail_text=audit_trail_text,
    )

    # System prompt is a short framing message; full instructions are in the
    # user message (the rendered prompt).  This matches the extraction agent pattern.
    system_prompt = (
        "You are an AP triage analyst. Follow the instructions in the user message exactly. "
        "Return only valid JSON as specified."
    )

    # -- Step 6: Call the LLM --
    if llm_client is None:
        settings = get_settings()
        llm_client = OpenRouterClient(settings)

    try:
        raw_response = llm_client.complete(system_prompt, rendered_prompt)
    except LLMCallError as exc:
        raise InvestigationError(
            f"LLM call failed for invoice {invoice_id!r}: {exc}"
        ) from exc

    # -- Step 7: Parse and validate --
    investigation = _parse_llm_response(raw_response)

    # -- Step 8: Write audit event --
    _write_investigation_completed(invoice_id, vendor_name, investigation)

    logger.info(
        "Exception investigation completed",
        extra={
            "invoice_id": invoice_id,
            "recommended_action": investigation.recommended_action,
            "confidence": investigation.confidence,
        },
    )

    # -- Step 9: Return --
    return investigation
