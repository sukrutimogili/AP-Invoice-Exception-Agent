"""
routing/decision.py — STP eligibility and exception routing.

Requirements.md FR-3.1 (implemented verbatim):
  An invoice is eligible for STP only if ALL of:
    1. extraction is valid                      (pre-condition — caller's responsibility)
    2. vendor is known                          → vendor_known
    3. PO resolves                              → po_resolved
    4. contract resolves                        → contract_resolved
    5. quantities match                         → quantities_match
    6. unit prices match within tolerance       → prices_match
    7. total matches within tolerance           → total_matches
    8. invoice is under threshold OR approval
       is on file                               → approval_satisfied

FR-4.1: any invoice failing one or more checks → ExceptionRecord, never paid.
FR-4.2: each ExceptionRecord carries one or more structured reason codes with
        supporting data.
FR-4.3: human approve-override / reject handled in api/exceptions.py.
FR-3.2: STP invoices receive a PaymentSchedule automatically.
FR-3.3: every STP decision writes an audit record (caller's responsibility via
        audit/writer.py in Phase 5 — this module returns the typed results only).

Design:
  - Pure functions, no I/O.
  - RoutingDecision — the typed output; either STP or EXCEPTION.
  - route(match_result, invoice) → RoutingDecision
  - Each failing sub-condition maps to exactly one ExceptionReasonCode.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Literal, Union

from pydantic import BaseModel, Field

from models.enums import ExceptionReasonCode, ExceptionStatus
from models.exception_record import ExceptionRecordCreate, ExceptionReasonSchema
from models.invoice import InvoiceCreate
from models.match_result import MatchResultCreate
from models.payment_schedule import PaymentScheduleCreate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing output types
# ---------------------------------------------------------------------------


class STPDecision(BaseModel):
    """
    Invoice passed all FR-3.1 checks — eligible for straight-through processing.

    Carries a ready-to-persist PaymentSchedule (FR-3.2).
    """

    outcome: Literal["STP"] = "STP"
    invoice_id: str
    payment_schedule: PaymentScheduleCreate


class ExceptionDecision(BaseModel):
    """
    Invoice failed one or more FR-3.1 checks — routed to the human queue.

    Carries a ready-to-persist ExceptionRecord with all reason codes (FR-4.2).
    Never carries a PaymentSchedule — structural enforcement of FR-4.1.
    """

    outcome: Literal["EXCEPTION"] = "EXCEPTION"
    invoice_id: str
    exception_record: ExceptionRecordCreate


# Union type returned by route()
RoutingDecision = Union[STPDecision, ExceptionDecision]


# ---------------------------------------------------------------------------
# Reason-code derivation (FR-4.2)
# ---------------------------------------------------------------------------

# Maps each MatchResult boolean field to the reason code raised when it is False.
_CONDITION_TO_REASON: list[tuple[str, ExceptionReasonCode]] = [
    ("vendor_known",        ExceptionReasonCode.UNKNOWN_VENDOR),
    ("po_resolved",         ExceptionReasonCode.PO_NOT_FOUND),
    ("contract_resolved",   ExceptionReasonCode.CONTRACT_NOT_FOUND),
    ("quantities_match",    ExceptionReasonCode.QTY_MISMATCH),
    ("prices_match",        ExceptionReasonCode.PRICE_VARIANCE),
    ("total_matches",       ExceptionReasonCode.TOTAL_MISMATCH),
    ("approval_satisfied",  ExceptionReasonCode.MISSING_APPROVAL),
]


def _derive_reasons(
    match_result: MatchResultCreate,
) -> list[ExceptionReasonSchema]:
    """
    Walk every FR-3.1 sub-condition and collect a reason for each failure.

    supporting_data is populated with the numeric evidence available from the
    MatchResult (variance amounts, line-level details, etc.) so that the human
    reviewer has full context without needing to re-run the engine.
    """
    reasons: list[ExceptionReasonSchema] = []

    for field_name, reason_code in _CONDITION_TO_REASON:
        if getattr(match_result, field_name):
            continue  # check passed — no reason to add

        supporting: dict = {}

        if reason_code == ExceptionReasonCode.PRICE_VARIANCE:
            # Attach per-line price evidence.
            supporting["line_variances"] = [
                {
                    "line_number": d.line_number,
                    "billed_unit_price": str(d.billed_unit_price),
                    "contract_unit_price": str(d.contract_unit_price),
                    "price_variance_abs": str(d.price_variance_abs),
                    "price_variance_pct": str(d.price_variance_pct),
                }
                for d in match_result.line_item_details
                if not d.price_match
            ]

        elif reason_code == ExceptionReasonCode.QTY_MISMATCH:
            supporting["line_variances"] = [
                {
                    "line_number": d.line_number,
                    "billed_qty": str(d.billed_qty),
                    "ordered_qty": str(d.ordered_qty),
                    "qty_variance_abs": str(d.qty_variance_abs),
                }
                for d in match_result.line_item_details
                if not d.qty_match
            ]

        elif reason_code == ExceptionReasonCode.TOTAL_MISMATCH:
            if match_result.total_variance_abs is not None:
                supporting["total_variance_abs"] = str(match_result.total_variance_abs)
            if match_result.total_variance_pct is not None:
                supporting["total_variance_pct"] = str(match_result.total_variance_pct)

        elif reason_code == ExceptionReasonCode.MISSING_APPROVAL:
            supporting["note"] = (
                "Invoice total is at or above the approval threshold "
                "and no approval is on file."
            )

        reasons.append(
            ExceptionReasonSchema(
                reason_code=reason_code,
                supporting_data=supporting,
            )
        )

    return reasons


# ---------------------------------------------------------------------------
# Main routing function
# ---------------------------------------------------------------------------


def route(
    match_result: MatchResultCreate,
    invoice: InvoiceCreate,
    invoice_id: str | None = None,
    payment_due_date: date | None = None,
) -> RoutingDecision:
    """
    Apply the FR-3.1 eligibility rule and return a routing decision.

    Args:
        match_result:     Output of MatchingEngine.run() for this invoice.
        invoice:          The validated InvoiceCreate (used for payment amount
                          and due date).
        invoice_id:       Stable ID for the invoice record.  Defaults to
                          match_result.invoice_id if not supplied.
        payment_due_date: Override the scheduled payment date.  Defaults to
                          invoice.due_date (standard terms).

    Returns:
        STPDecision      — if ALL FR-3.1 sub-conditions pass.
        ExceptionDecision — if ANY FR-3.1 sub-condition fails (FR-4.1).
    """
    inv_id = invoice_id or match_result.invoice_id
    due = payment_due_date or invoice.due_date

    if match_result.overall_passed:
        # -------------------------------------------------------------------
        # FR-3.1 all checks passed → STP (FR-3.2)
        # -------------------------------------------------------------------
        schedule = PaymentScheduleCreate(
            invoice_id=inv_id,
            scheduled_date=due,
            amount=invoice.grand_total,
            discount_taken=False,
        )
        logger.info(
            "Invoice routed STP",
            extra={"invoice_id": inv_id, "amount": str(invoice.grand_total)},
        )
        return STPDecision(invoice_id=inv_id, payment_schedule=schedule)

    # -----------------------------------------------------------------------
    # One or more checks failed → EXCEPTION (FR-4.1)
    # -----------------------------------------------------------------------
    reasons = _derive_reasons(match_result)

    # Safety: _derive_reasons must always produce at least one reason when
    # overall_passed is False (otherwise overall_passed would be True).
    if not reasons:
        # Defensive fallback — should never occur in practice.
        reasons = [
            ExceptionReasonSchema(
                reason_code=ExceptionReasonCode.OFF_CONTRACT_TERM,
                supporting_data={"note": "Unknown failure — see match result."},
            )
        ]

    exception_record = ExceptionRecordCreate(
        invoice_id=inv_id,
        reasons=reasons,
        status=ExceptionStatus.OPEN,
    )
    logger.info(
        "Invoice routed EXCEPTION",
        extra={
            "invoice_id": inv_id,
            "reason_codes": [r.reason_code.value for r in reasons],
        },
    )
    return ExceptionDecision(invoice_id=inv_id, exception_record=exception_record)
