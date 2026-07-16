"""
audit/writer.py — Single append-only write path for all audit events.

spec.md Phase 5 / requirements.md FR-6:
  "No module should write AuditEvent rows directly except through this module."

Design:
  - One public function per state transition (write_invoice_received,
    write_extraction_succeeded, etc.) — callers never construct AuditEventCreate
    themselves.
  - Append-only enforced at this layer: no update() or delete() function exists.
    The only mutation allowed is appending a new event to the store.
  - In-process store (list-based) for Phase 5; Phase 9 will swap the backing
    store for a DB session without changing any caller.
  - Every function is idempotent in the sense that calling it twice with the
    same inputs produces two audit records (correct — reprocessing must be
    visible in the trail, not silently deduplicated).

State transitions covered (AuditEventType):
  INVOICE_RECEIVED          — invoice has been ingested.
  EXTRACTION_SUCCEEDED      — LLM extraction produced a valid InvoiceCreate.
  EXTRACTION_FAILED         — extraction failed; invoice flagged NEEDS_REEXTRACTION.
  MATCHING_COMPLETED        — MatchingEngine produced a MatchResultCreate.
  STP_APPROVED              — all FR-3.1 checks passed; PaymentSchedule created.
  EXCEPTION_RAISED          — one or more FR-3.1 checks failed; routed to human.
  HUMAN_OVERRIDE_APPROVED   — human approved-with-override (FR-4.3).
  HUMAN_REJECTED            — human rejected the invoice (FR-4.3).
  PAYMENT_SCHEDULED         — payment scheduled (written alongside STP_APPROVED).
  DISCOUNT_EVALUATED        — discount recommendation computed (FR-7).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from models.audit_event import AuditEventCreate, AuditEventRead
from models.enums import AuditEventType
from models.exception_record import ExceptionRecordCreate
from models.invoice import InvoiceCreate
from models.match_result import MatchResultCreate
from models.payment_schedule import PaymentScheduleCreate
from extraction.schemas import ExtractionFailure, ExtractionSuccess

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process append-only store.
#
# Stored as a list of dicts (the serialised AuditEventRead shape) so query
# helpers can filter without needing Pydantic round-trips on every read.
# Keyed internally by auto-assigned UUID; never mutated after appending.
# ---------------------------------------------------------------------------
_audit_log: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# Internal helpers (not exported)
# ---------------------------------------------------------------------------


def _append(event: AuditEventCreate) -> AuditEventRead:
    """
    Append one AuditEventCreate to the store and return the AuditEventRead.

    This is the ONLY place data enters the audit log.  All public write_*
    functions call _append — nothing else does.

    Raises:
        Nothing — audit writes must never crash the main pipeline.  Errors are
        logged at ERROR level and silently swallowed so a logging failure cannot
        block payment processing.  This is intentional per spec.md §3
        (reliability: every failure mode maps to a typed outcome — audit failure
        is logged and swallowed, not propagated).
    """
    try:
        record = AuditEventRead(
            id=str(uuid.uuid4()),
            created_at=datetime.now(tz=timezone.utc),
            invoice_id=event.invoice_id,
            invoice_number=event.invoice_number,
            vendor_name=event.vendor_name,
            po_reference=event.po_reference,
            event_type=event.event_type,
            payload_json=event.payload_json,
            actor_id=event.actor_id,
        )
        _audit_log.append(record.model_dump())
        logger.debug(
            "Audit event written",
            extra={
                "event_id": record.id,
                "invoice_id": event.invoice_id,
                "event_type": event.event_type.value,
            },
        )
        return record
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Audit write failed — event dropped",
            extra={"event_type": str(event.event_type), "error": str(exc)},
        )
        # Return a minimal record so callers that log the return value don't crash.
        return AuditEventRead(
            id="error",
            created_at=datetime.now(tz=timezone.utc),
            invoice_id=event.invoice_id,
            invoice_number=event.invoice_number,
            vendor_name=event.vendor_name,
            po_reference=event.po_reference,
            event_type=event.event_type,
            payload_json=None,
            actor_id=event.actor_id,
        )


def _payload(**kwargs: Any) -> str:
    """Serialise keyword args to a compact JSON string for payload_json."""
    return json.dumps(kwargs, default=str)


# ---------------------------------------------------------------------------
# Store management (used by tests to reset between scenarios)
# ---------------------------------------------------------------------------


def clear_audit_log() -> None:
    """
    Clear all audit events from the in-process store.

    Only for use in tests.  Never call from application code.
    """
    _audit_log.clear()


def get_all_events() -> list[dict[str, Any]]:
    """Return a shallow copy of all audit log entries (immutable originals)."""
    return list(_audit_log)


# ---------------------------------------------------------------------------
# Public write_* functions — one per state transition
# ---------------------------------------------------------------------------


def write_invoice_received(
    invoice_id: str,
    invoice_number: str,
    vendor_name: str | None = None,
    po_reference: str | None = None,
    source_file_ref: str | None = None,
) -> AuditEventRead:
    """
    Write INVOICE_RECEIVED — invoice has been ingested (FR-6.1).

    Args:
        invoice_id:      Stable ID for the invoice record.
        invoice_number:  Invoice number from the document.
        vendor_name:     Vendor name (if already known).
        po_reference:    PO reference (if already known).
        source_file_ref: Optional storage key for the source file.
    """
    return _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.INVOICE_RECEIVED,
            invoice_number=invoice_number,
            vendor_name=vendor_name,
            po_reference=po_reference,
            payload_json=_payload(
                invoice_number=invoice_number,
                source_file_ref=source_file_ref,
            ),
        )
    )


def write_extraction_succeeded(
    invoice_id: str,
    result: ExtractionSuccess,
) -> AuditEventRead:
    """
    Write EXTRACTION_SUCCEEDED — LLM produced a valid InvoiceCreate (FR-6.1).

    Args:
        invoice_id: Stable ID for the invoice record.
        result:     The ExtractionSuccess produced by ExtractionAgent.extract().
    """
    inv = result.invoice
    return _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.EXTRACTION_SUCCEEDED,
            invoice_number=inv.invoice_number,
            vendor_name=inv.vendor_name,
            po_reference=inv.po_reference,
            payload_json=_payload(
                invoice_number=inv.invoice_number,
                vendor_name=inv.vendor_name,
                po_reference=inv.po_reference,
                contract_reference=inv.contract_reference,
                grand_total=str(inv.grand_total),
                attempt_count=result.attempt_count,
                extraction_status=result.extraction_status.value,
                raw_payload_length=len(result.raw_payload) if result.raw_payload else 0,
            ),
        )
    )


def write_extraction_failed(
    invoice_id: str,
    result: ExtractionFailure,
    invoice_number: str | None = None,
) -> AuditEventRead:
    """
    Write EXTRACTION_FAILED — invoice flagged NEEDS_REEXTRACTION (FR-5.1, FR-6.1).

    Args:
        invoice_id:     Stable ID for the invoice record.
        result:         The ExtractionFailure produced by ExtractionAgent.extract().
        invoice_number: Optional invoice number (may be unknown if extraction failed
                        before any fields were parsed).
    """
    return _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.EXTRACTION_FAILED,
            invoice_number=invoice_number,
            payload_json=_payload(
                reason=result.reason.value,
                error_detail=result.error_detail,
                attempt_count=result.attempt_count,
                extraction_status=result.extraction_status.value,
            ),
        )
    )


def write_matching_completed(
    invoice_id: str,
    match_result: MatchResultCreate,
) -> AuditEventRead:
    """
    Write MATCHING_COMPLETED — MatchingEngine produced a result (FR-6.1).

    The full field-by-field result is serialised into the payload so the
    audit trail is self-contained (FR-6.1: reconstructable without re-running).

    Args:
        invoice_id:   Stable ID for the invoice record.
        match_result: MatchResultCreate from MatchingEngine.run().
    """
    line_details = [
        {
            "line_number": d.line_number,
            "billed_qty": str(d.billed_qty),
            "ordered_qty": str(d.ordered_qty),
            "qty_match": d.qty_match,
            "billed_unit_price": str(d.billed_unit_price),
            "contract_unit_price": str(d.contract_unit_price),
            "price_variance_pct": str(d.price_variance_pct),
            "price_match": d.price_match,
        }
        for d in match_result.line_item_details
    ]
    return _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.MATCHING_COMPLETED,
            payload_json=_payload(
                overall_passed=match_result.overall_passed,
                vendor_known=match_result.vendor_known,
                po_resolved=match_result.po_resolved,
                contract_resolved=match_result.contract_resolved,
                quantities_match=match_result.quantities_match,
                prices_match=match_result.prices_match,
                total_matches=match_result.total_matches,
                approval_satisfied=match_result.approval_satisfied,
                total_variance_abs=str(match_result.total_variance_abs)
                    if match_result.total_variance_abs is not None else None,
                total_variance_pct=str(match_result.total_variance_pct)
                    if match_result.total_variance_pct is not None else None,
                line_item_details=line_details,
            ),
        )
    )


def write_stp_approved(
    invoice_id: str,
    invoice: InvoiceCreate,
    payment_schedule: PaymentScheduleCreate,
) -> AuditEventRead:
    """
    Write STP_APPROVED — all FR-3.1 checks passed; PaymentSchedule created.

    Args:
        invoice_id:       Stable ID for the invoice record.
        invoice:          The validated InvoiceCreate.
        payment_schedule: The PaymentScheduleCreate produced by route().
    """
    return _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.STP_APPROVED,
            invoice_number=invoice.invoice_number,
            vendor_name=invoice.vendor_name,
            po_reference=invoice.po_reference,
            payload_json=_payload(
                grand_total=str(invoice.grand_total),
                scheduled_date=str(payment_schedule.scheduled_date),
                payment_amount=str(payment_schedule.amount),
                discount_taken=payment_schedule.discount_taken,
            ),
        )
    )


def write_exception_raised(
    invoice_id: str,
    invoice: InvoiceCreate,
    exception_record: ExceptionRecordCreate,
) -> AuditEventRead:
    """
    Write EXCEPTION_RAISED — invoice routed to human queue (FR-4.1, FR-6.1).

    The full reason codes and supporting data are serialised so the auditor
    can reconstruct exactly which checks failed and why.

    Args:
        invoice_id:       Stable ID for the invoice record.
        invoice:          The validated InvoiceCreate.
        exception_record: The ExceptionRecordCreate produced by route().
    """
    reasons_payload = [
        {
            "reason_code": r.reason_code.value,
            "supporting_data": r.supporting_data,
        }
        for r in exception_record.reasons
    ]
    return _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.EXCEPTION_RAISED,
            invoice_number=invoice.invoice_number,
            vendor_name=invoice.vendor_name,
            po_reference=invoice.po_reference,
            payload_json=_payload(
                reason_codes=[r.reason_code.value for r in exception_record.reasons],
                reasons=reasons_payload,
                grand_total=str(invoice.grand_total),
            ),
        )
    )


def write_human_override_approved(
    invoice_id: str,
    actor_id: str,
    resolution_notes: str | None,
    reason_codes: list[str],
) -> AuditEventRead:
    """
    Write HUMAN_OVERRIDE_APPROVED — human approved-with-override (FR-4.3, FR-6.1).

    Args:
        invoice_id:       Stable ID for the invoice record.
        actor_id:         Identity of the human reviewer (required).
        resolution_notes: Optional free-text rationale.
        reason_codes:     The exception reason codes that were overridden.
    """
    return _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.HUMAN_OVERRIDE_APPROVED,
            actor_id=actor_id,
            payload_json=_payload(
                actor_id=actor_id,
                resolution_notes=resolution_notes,
                overridden_reason_codes=reason_codes,
            ),
        )
    )


def write_human_rejected(
    invoice_id: str,
    actor_id: str,
    resolution_notes: str | None,
    reason_codes: list[str],
) -> AuditEventRead:
    """
    Write HUMAN_REJECTED — human rejected the invoice back to vendor (FR-4.3, FR-6.1).

    Args:
        invoice_id:       Stable ID for the invoice record.
        actor_id:         Identity of the human reviewer (required).
        resolution_notes: Optional free-text rationale.
        reason_codes:     The exception reason codes that led to rejection.
    """
    return _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.HUMAN_REJECTED,
            actor_id=actor_id,
            payload_json=_payload(
                actor_id=actor_id,
                resolution_notes=resolution_notes,
                rejected_reason_codes=reason_codes,
            ),
        )
    )


def write_payment_scheduled(
    invoice_id: str,
    invoice: InvoiceCreate,
    payment_schedule: PaymentScheduleCreate,
) -> AuditEventRead:
    """
    Write PAYMENT_SCHEDULED — payment has been scheduled (FR-3.2, FR-6.1).

    Distinct from STP_APPROVED: STP_APPROVED records the routing decision;
    PAYMENT_SCHEDULED records the concrete scheduled payment entry.

    Args:
        invoice_id:       Stable ID for the invoice record.
        invoice:          The validated InvoiceCreate.
        payment_schedule: The PaymentScheduleCreate with the date and amount.
    """
    return _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.PAYMENT_SCHEDULED,
            invoice_number=invoice.invoice_number,
            vendor_name=invoice.vendor_name,
            po_reference=invoice.po_reference,
            payload_json=_payload(
                scheduled_date=str(payment_schedule.scheduled_date),
                amount=str(payment_schedule.amount),
                discount_taken=payment_schedule.discount_taken,
                discount_amount=str(payment_schedule.discount_amount)
                    if payment_schedule.discount_amount is not None else None,
            ),
        )
    )


def write_document_conflict_detected(
    invoice_id: str,
    invoice: InvoiceCreate,
    document_type: str,
    natural_key: str,
    diff: dict[str, Any],
) -> AuditEventRead:
    """
    Write DOCUMENT_CONFLICT_DETECTED — an uploaded PO or contract conflicts
    with the row already stored in the database for the same natural key.

    No write is performed to the PO/contract table; this event records that
    the conflict was observed and the invoice has been routed to EXCEPTION
    with reason code DOCUMENT_CONFLICT.

    Args:
        invoice_id:    Stable ID for the invoice being processed.
        invoice:       The validated InvoiceCreate (for correlation fields).
        document_type: "PO" or "CONTRACT".
        natural_key:   The conflicting key (po_number or contract_reference).
        diff:          Field-level diff dict {field_name: {"existing": x, "incoming": y}}.
                       Serialised verbatim into the payload_json for audit trail.
    """
    return _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.DOCUMENT_CONFLICT_DETECTED,
            invoice_number=invoice.invoice_number,
            vendor_name=invoice.vendor_name,
            po_reference=invoice.po_reference,
            payload_json=_payload(
                document_type=document_type,
                natural_key=natural_key,
                conflicting_fields=list(diff.keys()),
                diff=diff,
            ),
        )
    )


def write_discount_evaluated(
    invoice_id: str,
    invoice: InvoiceCreate,
    recommendation: str,
    discount_pct: str | None = None,
    annualized_return: str | None = None,
    hurdle_rate: str | None = None,
    discount_amount: str | None = None,
    window_days: int | None = None,
    note: str | None = None,
) -> AuditEventRead:
    """
    Write DISCOUNT_EVALUATED — discount recommendation computed (FR-7, FR-6.1).

    Args:
        invoice_id:        Stable ID for the invoice record.
        invoice:           The validated InvoiceCreate.
        recommendation:    One of DiscountRecommendation enum values as string.
        discount_pct:      Discount percentage (e.g. "0.02").
        annualized_return: Computed annualized return (e.g. "0.1459").
        hurdle_rate:       Configured hurdle rate (e.g. "0.10").
        discount_amount:   Dollar amount of discount available.
        window_days:       Days within which payment qualifies.
        note:              Optional human-readable note (e.g. "DISCOUNT_WINDOW_MISSED").
    """
    return _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.DISCOUNT_EVALUATED,
            invoice_number=invoice.invoice_number,
            vendor_name=invoice.vendor_name,
            po_reference=invoice.po_reference,
            payload_json=_payload(
                recommendation=recommendation,
                discount_pct=discount_pct,
                annualized_return=annualized_return,
                hurdle_rate=hurdle_rate,
                discount_amount=discount_amount,
                window_days=window_days,
                note=note,
            ),
        )
    )
