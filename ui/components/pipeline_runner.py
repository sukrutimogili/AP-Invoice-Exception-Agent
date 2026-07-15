"""
ui/components/pipeline_runner.py — Thin adapter between the Streamlit pages
and the invoice processing service.

Architecture:
  Streamlit pages → pipeline_runner → services/invoice_service
                                    → domain modules (matching, routing, …)

This module imports nothing from FastAPI.  The import chain is:

  ui/components/pipeline_runner.py
    └── services/invoice_service.py   (run_pipeline, InvoiceProcessingResult)
          └── domain modules only     (matching, routing, discount, audit, …)

The PipelineResult dataclass produced here is the UI-specific view of the
service result — it carries additional display fields (invoice_fields,
match_checks, discount_detail) that the service layer doesn't need to know
about.  The adapter pattern keeps the service layer clean.
"""

from __future__ import annotations

import json as _json
import uuid
from dataclasses import dataclass, field
from typing import Any

import audit.writer as audit_writer
from app.config import get_settings
from extraction.agent import ExtractionAgent
from extraction.llm_client import OpenRouterClient
from extraction.schemas import ExtractionFailure, ExtractionSuccess
from models.contract import ContractCreate
from models.enums import AuditEventType
from models.invoice import InvoiceCreate
from models.purchase_order import PurchaseOrderCreate
from models.vendor import VendorCreate
from services.invoice_service import InvoiceProcessingResult, run_pipeline


# ---------------------------------------------------------------------------
# UI-facing result type
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """
    UI-friendly view of an invoice processing result.

    Extends InvoiceProcessingResult with display-only fields that are
    extracted from the audit trail (match checks, discount detail) and the
    flattened invoice dict the pages render directly.
    """

    invoice_id: str
    invoice_number: str
    outcome: str  # "STP" | "EXCEPTION" | "NEEDS_REEXTRACTION" | "ERROR"

    # STP path
    payment_schedule: dict[str, Any] | None = None
    discount_recommendation: str | None = None

    # Exception path
    exception_reasons: list[str] = field(default_factory=list)

    # Extraction failure path
    extraction_failure_reason: str | None = None

    # Display-only fields — not present in InvoiceProcessingResult
    invoice_fields: dict[str, Any] | None = None
    match_checks: dict[str, bool] | None = None
    discount_detail: dict[str, Any] | None = None

    processed_at: str = ""
    error_message: str | None = None  # user-friendly; never a stack trace


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _invoice_to_dict(invoice: InvoiceCreate) -> dict[str, Any]:
    """Flatten an InvoiceCreate into a display-ready plain dict."""
    return {
        "invoice_number": invoice.invoice_number,
        "vendor_name": invoice.vendor_name,
        "invoice_date": str(invoice.invoice_date),
        "po_reference": invoice.po_reference,
        "contract_reference": invoice.contract_reference,
        "payment_terms": invoice.payment_terms,
        "subtotal": str(invoice.subtotal),
        "tax": str(invoice.tax),
        "grand_total": str(invoice.grand_total),
        "due_date": str(invoice.due_date),
        "line_items": [
            {
                "line": item.line_number,
                "description": item.description,
                "qty": str(item.qty),
                "unit_price": str(item.unit_price),
                "amount": str(item.amount),
            }
            for item in invoice.line_items
        ],
    }


def _extract_match_checks(invoice_id: str) -> dict[str, bool] | None:
    """
    Read the MATCHING_COMPLETED audit event and return per-check booleans.
    Returns None if no event is found for this invoice.
    """
    events = audit_writer.get_all_events()
    for e in reversed(events):
        if (
            e.get("invoice_id") == invoice_id
            and e.get("event_type") == AuditEventType.MATCHING_COMPLETED.value
        ):
            raw = e.get("payload_json")
            if raw:
                try:
                    p = _json.loads(raw)
                    return {
                        "Vendor Known":       p.get("vendor_known", False),
                        "PO Resolved":        p.get("po_resolved", False),
                        "Contract Resolved":  p.get("contract_resolved", False),
                        "Quantities Match":   p.get("quantities_match", False),
                        "Prices Match":       p.get("prices_match", False),
                        "Total Matches":      p.get("total_matches", False),
                        "Approval Satisfied": p.get("approval_satisfied", False),
                    }
                except Exception:
                    pass
    return None


def _extract_discount_detail(invoice_id: str) -> dict[str, Any] | None:
    """
    Read the DISCOUNT_EVALUATED audit event and return the payload dict.
    Returns None if no event is found for this invoice.
    """
    events = audit_writer.get_all_events()
    for e in reversed(events):
        if (
            e.get("invoice_id") == invoice_id
            and e.get("event_type") == AuditEventType.DISCOUNT_EVALUATED.value
        ):
            raw = e.get("payload_json")
            if raw:
                try:
                    return _json.loads(raw)
                except Exception:
                    pass
    return None


def _from_service_result(
    result: InvoiceProcessingResult,
    invoice: InvoiceCreate | None,
    invoice_id: str,
) -> PipelineResult:
    """Convert a service-layer result to a PipelineResult for the UI."""
    return PipelineResult(
        invoice_id=result.invoice_id,
        invoice_number=result.invoice_number,
        outcome=result.outcome,
        payment_schedule=result.payment_schedule,
        discount_recommendation=result.discount_recommendation,
        exception_reasons=result.exception_reasons,
        extraction_failure_reason=result.extraction_failure_reason,
        invoice_fields=_invoice_to_dict(invoice) if invoice is not None else None,
        match_checks=_extract_match_checks(invoice_id),
        discount_detail=_extract_discount_detail(invoice_id),
        processed_at=result.processed_at,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_submit_pipeline(
    invoice: InvoiceCreate,
    vendor: VendorCreate | None,
    po: PurchaseOrderCreate | None,
    contract: ContractCreate | None,
    approval_on_file: bool = False,
    invoice_id: str | None = None,
) -> PipelineResult:
    """
    Run the full processing pipeline for a pre-extracted InvoiceCreate.

    Delegates to services.invoice_service.run_pipeline().
    Returns a PipelineResult the UI pages can render directly.

    Args:
        invoice:          Validated InvoiceCreate.
        vendor:           Optional resolved vendor (None → UNKNOWN_VENDOR).
        po:               Optional resolved PO (None → PO_NOT_FOUND).
        contract:         Optional resolved contract (None → CONTRACT_NOT_FOUND).
        approval_on_file: True if a manual approval record exists.
        invoice_id:       Stable ID; auto-generated if not supplied.

    Returns:
        PipelineResult with outcome, payment schedule, exception reasons,
        match checks, and discount detail.
    """
    inv_id = invoice_id or str(uuid.uuid4())

    try:
        result = run_pipeline(
            invoice_id=inv_id,
            invoice=invoice,
            vendor=vendor,
            po=po,
            contract=contract,
            approval_on_file=approval_on_file,
        )
    except Exception as exc:
        return PipelineResult(
            invoice_id=inv_id,
            invoice_number=invoice.invoice_number,
            outcome="ERROR",
            error_message=f"Pipeline error: {type(exc).__name__}: {exc}",
        )

    return _from_service_result(result, invoice, inv_id)


def run_extraction_pipeline(invoice_text: str) -> PipelineResult:
    """
    Run LLM extraction + full processing pipeline for a raw text document.

    Runs ExtractionAgent.extract() then delegates to
    services.invoice_service.run_pipeline().

    Args:
        invoice_text: Raw UTF-8 text of the invoice document.

    Returns:
        PipelineResult.  If extraction fails, outcome='NEEDS_REEXTRACTION'.
    """
    settings = get_settings()
    invoice_id = str(uuid.uuid4())

    try:
        llm_client = OpenRouterClient(settings=settings)
        agent = ExtractionAgent(llm_client=llm_client)
        extraction_result = agent.extract(invoice_text)
    except Exception as exc:
        return PipelineResult(
            invoice_id=invoice_id,
            invoice_number="(extraction error)",
            outcome="ERROR",
            error_message=f"LLM extraction failed: {type(exc).__name__}: {exc}",
        )

    if isinstance(extraction_result, ExtractionFailure):
        audit_writer.write_invoice_received(
            invoice_id=invoice_id,
            invoice_number="(unknown — extraction failed)",
        )
        audit_writer.write_extraction_failed(
            invoice_id=invoice_id,
            result=extraction_result,
        )
        return PipelineResult(
            invoice_id=invoice_id,
            invoice_number="(extraction failed)",
            outcome="NEEDS_REEXTRACTION",
            extraction_failure_reason=extraction_result.reason.value,
        )

    assert isinstance(extraction_result, ExtractionSuccess)
    audit_writer.write_extraction_succeeded(invoice_id=invoice_id, result=extraction_result)

    # Downstream pipeline — no entity resolution on the upload path (TBD: DB).
    try:
        result = run_pipeline(
            invoice_id=invoice_id,
            invoice=extraction_result.invoice,
            vendor=None,    # TBD: resolve from DB
            po=None,        # TBD: resolve from DB
            contract=None,  # TBD: resolve from DB
            approval_on_file=False,
        )
    except Exception as exc:
        return PipelineResult(
            invoice_id=invoice_id,
            invoice_number=extraction_result.invoice.invoice_number,
            outcome="ERROR",
            error_message=f"Pipeline error after extraction: {type(exc).__name__}: {exc}",
        )

    return _from_service_result(result, extraction_result.invoice, invoice_id)
