"""
services/invoice_service.py — Invoice processing pipeline service.

spec.md §3 (Engineering Standards):
  "Business logic lives in a domain/ or services/ layer — never inside
  FastAPI route handlers."

This module is the single implementation of the invoice processing pipeline.
It contains no FastAPI, no Streamlit, no HTTP concepts — only domain logic.

Both callers import from here:
  api/invoices.py          — the FastAPI HTTP layer (routes/handlers)
  ui/components/pipeline_runner.py — the Streamlit presentation layer

Public API
----------
run_pipeline(invoice_id, invoice, vendor, po, contract, approval_on_file)
    → InvoiceProcessingResult

InvoiceProcessingResult  — typed result dataclass; replaces the old
    InvoiceSubmitResponse that was defined inside the API layer.

Pipeline steps (unchanged from Phase 9):
  1. INVOICE_RECEIVED audit event
  2. MatchingEngine.run()
  3. MATCHING_COMPLETED audit event
  4. route() → STPDecision | ExceptionDecision
  5a. STP:       register payment schedule, STP_APPROVED + PAYMENT_SCHEDULED
                 audit events, evaluate_discount(), DISCOUNT_EVALUATED audit
  5b. EXCEPTION: register exception, EXCEPTION_RAISED audit event
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import audit.writer as audit
from app.config import get_settings
from services.exception_store import register_exception
from services.payment_store import register_payment_schedule
from discount.calculator import DiscountGateError, evaluate_discount
from discount.parser import parse_discount_term
from matching.engine import MatchInput, MatchingEngine
from models.contract import ContractCreate, DiscountTermSchema
from models.invoice import InvoiceCreate
from models.purchase_order import PurchaseOrderCreate
from models.vendor import VendorCreate
from routing.decision import ExceptionDecision, STPDecision, route

logger = logging.getLogger(__name__)

# Shared engine instance — stateless, safe to reuse across calls.
_engine = MatchingEngine()


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class InvoiceProcessingResult:
    """
    Typed result of run_pipeline().

    Carries everything both the API response serialiser and the UI renderer
    need, without depending on either FastAPI or Streamlit types.
    """

    invoice_id: str
    invoice_number: str

    # "STP" | "EXCEPTION" | "NEEDS_REEXTRACTION"
    outcome: str

    # STP path
    payment_schedule: dict[str, Any] | None = None
    discount_recommendation: str | None = None

    # EXCEPTION path
    exception_reasons: list[str] = field(default_factory=list)

    # NEEDS_REEXTRACTION path
    extraction_failure_reason: str | None = None

    processed_at: str = ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_pipeline(
    *,
    invoice_id: str,
    invoice: InvoiceCreate,
    vendor: VendorCreate | None,
    po: PurchaseOrderCreate | None,
    contract: ContractCreate | None,
    approval_on_file: bool,
) -> InvoiceProcessingResult:
    """
    Run the full invoice processing pipeline for one validated InvoiceCreate.

    This function contains no business logic — all rules live in the domain
    modules it calls.  It is the sole implementation of the processing
    pipeline; the API layer and UI layer both delegate here.

    Steps:
      1. Write INVOICE_RECEIVED audit event.
      2. Run MatchingEngine.
      3. Write MATCHING_COMPLETED audit event.
      4. Call route() → STPDecision | ExceptionDecision.
      5a. STP:       register payment schedule, write STP_APPROVED +
                     PAYMENT_SCHEDULED audit events.
                     Evaluate discount (FR-7.4: only for STP).
                     Write DISCOUNT_EVALUATED audit event.
      5b. EXCEPTION: register exception record, write EXCEPTION_RAISED.

    Args:
        invoice_id:       Stable ID for this invoice (caller must supply).
        invoice:          Validated InvoiceCreate.
        vendor:           Resolved VendorCreate, or None → UNKNOWN_VENDOR.
        po:               Resolved PurchaseOrderCreate, or None → PO_NOT_FOUND.
        contract:         Resolved ContractCreate, or None → CONTRACT_NOT_FOUND.
        approval_on_file: True if a manual approval record is on file.

    Returns:
        InvoiceProcessingResult with the routing outcome and relevant details.

    Raises:
        Does not raise — all errors are caught and returned as an ERROR outcome
        by the caller (run_pipeline itself lets domain exceptions propagate so
        the caller can decide how to present them: HTTPException vs UI message).
    """
    settings = get_settings()
    processed_at = datetime.utcnow().isoformat() + "Z"

    # -----------------------------------------------------------------------
    # Step 1 — INVOICE_RECEIVED
    # -----------------------------------------------------------------------
    audit.write_invoice_received(
        invoice_id=invoice_id,
        invoice_number=invoice.invoice_number,
        vendor_name=invoice.vendor_name,
        po_reference=invoice.po_reference,
    )

    # -----------------------------------------------------------------------
    # Step 2 — Matching
    # -----------------------------------------------------------------------
    tolerance = Decimal(str(settings.match_tolerance_percent))
    match_input = MatchInput(
        invoice=invoice,
        purchase_order=po,
        contract=contract,
        vendor=vendor,
        approval_on_file=approval_on_file,
        tolerance_pct=tolerance,
        invoice_id=invoice_id,
    )
    match_result = _engine.run(match_input)

    # -----------------------------------------------------------------------
    # Step 3 — MATCHING_COMPLETED
    # -----------------------------------------------------------------------
    audit.write_matching_completed(invoice_id=invoice_id, match_result=match_result)

    # -----------------------------------------------------------------------
    # Step 4 — Route
    # -----------------------------------------------------------------------
    decision = route(
        match_result=match_result,
        invoice=invoice,
        invoice_id=invoice_id,
        payment_due_date=invoice.due_date,
    )

    # -----------------------------------------------------------------------
    # Step 5a — STP path
    # -----------------------------------------------------------------------
    if isinstance(decision, STPDecision):
        register_payment_schedule(decision.payment_schedule)

        audit.write_stp_approved(
            invoice_id=invoice_id,
            invoice=invoice,
            payment_schedule=decision.payment_schedule,
        )
        audit.write_payment_scheduled(
            invoice_id=invoice_id,
            invoice=invoice,
            payment_schedule=decision.payment_schedule,
        )

        # -------------------------------------------------------------------
        # FR-7.4 gate — discount only on STP invoices
        # -------------------------------------------------------------------
        discount_recommendation_value: str | None = None
        discount_term: DiscountTermSchema | None = None

        if contract is not None:
            if contract.discount_term is not None:
                discount_term = contract.discount_term
            else:
                raw_str = getattr(contract, "discount_term_raw", None)
                if raw_str:
                    discount_term = parse_discount_term(raw_str)

        hurdle_rate = Decimal(str(settings.discount_hurdle_rate_default))

        try:
            discount_rec = evaluate_discount(
                invoice_id=invoice_id,
                invoice_amount=invoice.grand_total,
                invoice_date=invoice.invoice_date,
                discount_term=discount_term,
                hurdle_rate=hurdle_rate,
                processing_date=date.today(),
                is_stp_eligible=True,
            )
            discount_recommendation_value = discount_rec.recommendation.value

            audit.write_discount_evaluated(
                invoice_id=invoice_id,
                invoice=invoice,
                recommendation=discount_rec.recommendation.value,
                discount_pct=str(discount_rec.discount_pct)
                if discount_rec.discount_pct is not None else None,
                annualized_return=str(discount_rec.annualized_return)
                if discount_rec.annualized_return is not None else None,
                hurdle_rate=str(discount_rec.hurdle_rate),
                discount_amount=str(discount_rec.discount_amount)
                if discount_rec.discount_amount is not None else None,
                window_days=discount_rec.discount_days,
                note="DISCOUNT_WINDOW_MISSED" if discount_rec.window_missed else None,
            )

        except DiscountGateError:
            # Should never occur on the STP path (is_stp_eligible=True).
            # Log at CRITICAL; do not abort — payment schedule must not be
            # rolled back because of a discount evaluation bug.
            logger.critical(
                "DiscountGateError raised on STP path — this is a code bug",
                extra={"invoice_id": invoice_id},
            )

        logger.info(
            "Invoice processed: STP",
            extra={
                "invoice_id": invoice_id,
                "invoice_number": invoice.invoice_number,
                "scheduled_date": str(decision.payment_schedule.scheduled_date),
                "discount_recommendation": discount_recommendation_value,
            },
        )

        return InvoiceProcessingResult(
            invoice_id=invoice_id,
            invoice_number=invoice.invoice_number,
            outcome="STP",
            payment_schedule={
                "invoice_id": invoice_id,
                "scheduled_date": str(decision.payment_schedule.scheduled_date),
                "amount": str(decision.payment_schedule.amount),
                "discount_taken": decision.payment_schedule.discount_taken,
                "discount_amount": str(decision.payment_schedule.discount_amount)
                if decision.payment_schedule.discount_amount is not None else None,
            },
            discount_recommendation=discount_recommendation_value,
            processed_at=processed_at,
        )

    # -----------------------------------------------------------------------
    # Step 5b — EXCEPTION path
    # -----------------------------------------------------------------------
    assert isinstance(decision, ExceptionDecision)

    register_exception(decision)
    audit.write_exception_raised(
        invoice_id=invoice_id,
        invoice=invoice,
        exception_record=decision.exception_record,
    )

    reason_codes = [r.reason_code.value for r in decision.exception_record.reasons]

    logger.info(
        "Invoice processed: EXCEPTION",
        extra={
            "invoice_id": invoice_id,
            "invoice_number": invoice.invoice_number,
            "reason_codes": reason_codes,
        },
    )

    return InvoiceProcessingResult(
        invoice_id=invoice_id,
        invoice_number=invoice.invoice_number,
        outcome="EXCEPTION",
        exception_reasons=reason_codes,
        processed_at=processed_at,
    )
