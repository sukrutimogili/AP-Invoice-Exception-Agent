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

Document-conflict flow
----------------------
When the UI uploads a PO or contract document alongside the invoice:

1.  Each document is extracted independently via its own agent.
2.  Extraction failure on PO/contract does NOT block the invoice — None is
    passed to run_pipeline() just as today (matching engine flags PO_NOT_FOUND
    or CONTRACT_NOT_FOUND as before).
3.  After successful extraction, upsert_po() / upsert_contract() is called.
    - UpsertCreated / UpsertUnchanged → proceed normally.
    - UpsertConflict → the invoice is immediately short-circuited to EXCEPTION
      with reason code DOCUMENT_CONFLICT.  The existing DB row is NOT
      overwritten.  A DOCUMENT_CONFLICT_DETECTED audit event is written with
      the field-level diff.  The diff is also stored on PipelineResult so the
      UI can render the Document Conflict expander.
"""

from __future__ import annotations

import json as _json
import uuid
from dataclasses import dataclass, field
from typing import Any

import audit.writer as audit_writer
from app.config import get_settings
from db.resolver import resolve_invoice_entities
from db.session import get_session
from extraction.agent import (
    ContractExtractionAgent,
    ExtractionAgent,
    PurchaseOrderExtractionAgent,
)
from extraction.contract_schemas import ContractExtractionFailure, ContractExtractionSuccess
from extraction.llm_client import OpenRouterClient
from extraction.po_schemas import POExtractionFailure, POExtractionSuccess
from extraction.schemas import ExtractionFailure, ExtractionSuccess
from models.contract import ContractCreate
from models.enums import AuditEventType, ExceptionReasonCode
from models.exception_record import ExceptionReasonSchema, ExceptionRecordCreate
from models.enums import ExceptionStatus
from models.invoice import InvoiceCreate
from models.purchase_order import PurchaseOrderCreate
from models.vendor import VendorCreate, VendorORM
from repositories.contract_repo import upsert_contract
from repositories.po_repo import upsert_po
from repositories.upsert_result import FieldDiff, UpsertConflict, UpsertCreated, UpsertUnchanged
from repositories.vendor_repo import get_vendor_by_code, upsert_vendor
from routing.decision import ExceptionDecision
from services.exception_store import register_exception
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

    Document-conflict fields
    ------------------------
    po_conflict_diff / contract_conflict_diff are populated when the uploaded
    PO or contract clashes with an existing DB row.  Each entry is a
    dict[str, dict] mapping field_name → {"existing": ..., "incoming": ...}.
    When present, outcome is always "EXCEPTION" with reason DOCUMENT_CONFLICT.
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

    # Document-conflict diffs — populated only when upsert returns UpsertConflict.
    # Shape: {field_name: {"existing": value, "incoming": value}}
    po_conflict_diff: dict[str, dict[str, Any]] | None = None
    contract_conflict_diff: dict[str, dict[str, Any]] | None = None

    # Extraction warnings — set when a PO or contract document was supplied but
    # extraction failed (either a typed POExtractionFailure/ContractExtractionFailure
    # or an unexpected exception).  None means "no document was uploaded" OR
    # "extraction succeeded".  A non-None value is always user-readable.
    po_extraction_warning: str | None = None
    contract_extraction_warning: str | None = None

    # Low-confidence fields — populated when invoice extraction succeeded but one
    # or more fields were flagged "low" confidence by the LLM.  The invoice is
    # routed to EXCEPTION (LOW_CONFIDENCE_EXTRACTION) so a human can verify the
    # values before proceeding to STP.
    # Shape: {field_name: extracted_value_as_string}
    # None means no low-confidence fields were reported (or extraction failed
    # before this check was reached).
    low_confidence_fields: dict[str, str] | None = None

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


def _diff_to_serialisable(diff: dict[str, FieldDiff]) -> dict[str, dict[str, Any]]:
    """Convert a {field: FieldDiff} dict to a JSON-serialisable dict for the UI."""
    return {
        k: {"existing": str(v.existing) if v.existing is not None else None,
            "incoming": str(v.incoming) if v.incoming is not None else None}
        for k, v in diff.items()
    }


def _collect_low_confidence_fields(
    extraction: ExtractionSuccess,
    invoice: InvoiceCreate,
) -> dict[str, str] | None:
    """
    Inspect field_confidence on a successful ExtractionSuccess.

    Returns a dict mapping each "low"-confidence field name to its extracted
    value (as a human-readable string), or None if all fields are high-confidence
    (including the default when field_confidence is empty).

    The value representation is best-effort — the goal is to give a human
    reviewer enough context to confirm or correct the reading.  Structured
    fields (line_items) are rendered compactly.
    """
    low_fields = {
        k: v
        for k, v in extraction.field_confidence.items()
        if v == "low"
    }
    if not low_fields:
        return None

    # Build a flat value map from the invoice for display.
    invoice_dict = _invoice_to_dict(invoice)
    result: dict[str, str] = {}

    for field_name in low_fields:
        if field_name in invoice_dict:
            val = invoice_dict[field_name]
            result[field_name] = str(val) if val is not None else "(null)"
        elif field_name.startswith("line_items["):
            # e.g. "line_items[0].unit_price"
            result[field_name] = "(see line items)"
        else:
            result[field_name] = "(unknown field)"

    return result or None


from dataclasses import dataclass as _dc


@_dc(frozen=True)
class _VendorResolution:
    """
    Result of resolving-or-creating a vendor by code within a session.

    vendor_id      — the vendors.id UUID to use as the FK on PO/Contract rows.
    was_created    — True if a new vendor row was just INSERTed in this call;
                     False if an existing row was found.  Only when was_created
                     is True should the caller write a VENDOR_AUTO_CREATED audit
                     event — and only AFTER the enclosing session.commit() has
                     succeeded.
    vendor_code    — echoed back for use in the audit event payload.
    vendor_name    — echoed back for use in the audit event payload.
    warning        — non-None only when vendor resolution failed (UpsertConflict
                     or unexpected result); the PO/Contract upsert should be
                     skipped and this message surfaced to the user.
    """
    vendor_id: str | None
    was_created: bool
    vendor_code: str
    vendor_name: str
    warning: str | None


def _resolve_or_create_vendor(
    session,
    vendor_code: str,
    vendor_name: str,
    source_document: str,
) -> _VendorResolution:
    """
    Look up a vendor by code; auto-create if absent.

    1. Call get_vendor_by_code(session, vendor_code).
       - Found → query VendorORM by vendor_code to get the UUID; return it
         with was_created=False.
    2. Not found → construct VendorCreate and call upsert_vendor().
       - UpsertCreated → query VendorORM to get the new UUID; return it with
         was_created=True.
       - Anything else (UpsertConflict, etc.) → return warning, vendor_id=None.

    The caller is responsible for:
      - Only writing the VENDOR_AUTO_CREATED audit event AFTER the enclosing
        session.commit() has succeeded (was_created=True signals this).
      - Skipping the PO/Contract upsert when warning is non-None.

    Notes on UUID retrieval:
      VendorCreate (returned by get_vendor_by_code and upsert_vendor) does NOT
      carry the `id` field — that lives on VendorORM.  After confirming the
      vendor exists or was created, this function re-queries VendorORM by
      vendor_code to get the authoritative UUID.

    Args:
        session:         Active SQLAlchemy Session (caller owns commit/rollback).
        vendor_code:     Vendor code extracted from the document.
        vendor_name:     Vendor name to use if auto-creating.
        source_document: "PO" or "Contract" — for warning messages only.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    def _get_vendor_id(vc: str) -> str | None:
        """Re-query VendorORM to get the UUID for vendor_code=vc."""
        row = (
            session.query(VendorORM)
            .filter(VendorORM.vendor_code == vc)
            .first()
        )
        return row.id if row is not None else None

    # ── 1. Try existing row ──────────────────────────────────────────────────
    existing = get_vendor_by_code(session, vendor_code)
    if existing is not None:
        vid = _get_vendor_id(vendor_code)
        if vid is None:
            # Should never happen — vendor was just found
            return _VendorResolution(
                vendor_id=None,
                was_created=False,
                vendor_code=vendor_code,
                vendor_name=vendor_name,
                warning=(
                    f"{source_document} vendor '{vendor_code}' found by code but UUID "
                    f"could not be retrieved — possible data integrity issue."
                ),
            )
        return _VendorResolution(
            vendor_id=vid,
            was_created=False,
            vendor_code=vendor_code,
            vendor_name=vendor_name,
            warning=None,
        )

    # ── 2. Not found — auto-create ───────────────────────────────────────────
    _log.info(
        "Vendor not found — auto-creating",
        extra={"vendor_code": vendor_code, "source_document": source_document},
    )
    from models.vendor import VendorCreate as _VendorCreate
    new_vendor = _VendorCreate(
        vendor_code=vendor_code,
        name=vendor_name,
        is_active=True,
        contact_email=None,
        notes=(
            f"Auto-created from {source_document} upload "
            f"— vendor_code not previously known"
        ),
    )
    result = upsert_vendor(session, new_vendor)
    if isinstance(result, UpsertCreated):
        vid = _get_vendor_id(vendor_code)
        if vid is None:
            return _VendorResolution(
                vendor_id=None,
                was_created=False,
                vendor_code=vendor_code,
                vendor_name=vendor_name,
                warning=(
                    f"{source_document} vendor '{vendor_code}' was inserted but UUID "
                    f"could not be retrieved — skipping {source_document} upsert."
                ),
            )
        return _VendorResolution(
            vendor_id=vid,
            was_created=True,
            vendor_code=vendor_code,
            vendor_name=vendor_name,
            warning=None,
        )

    # Unexpected result (UpsertConflict, UpsertUnchanged race, etc.)
    return _VendorResolution(
        vendor_id=None,
        was_created=False,
        vendor_code=vendor_code,
        vendor_name=vendor_name,
        warning=(
            f"{source_document} vendor '{vendor_code}' could not be resolved or "
            f"auto-created (upsert returned {type(result).__name__}) "
            f"— {source_document} not persisted."
        ),
    )


def _from_service_result(
    result: InvoiceProcessingResult,
    invoice: InvoiceCreate | None,
    invoice_id: str,
    po_conflict_diff: dict[str, dict[str, Any]] | None = None,
    contract_conflict_diff: dict[str, dict[str, Any]] | None = None,
    po_extraction_warning: str | None = None,
    contract_extraction_warning: str | None = None,
    low_confidence_fields: dict[str, str] | None = None,
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
        po_conflict_diff=po_conflict_diff,
        contract_conflict_diff=contract_conflict_diff,
        po_extraction_warning=po_extraction_warning,
        contract_extraction_warning=contract_extraction_warning,
        low_confidence_fields=low_confidence_fields,
        processed_at=result.processed_at,
    )


def _make_conflict_pipeline_result(
    invoice_id: str,
    invoice: InvoiceCreate,
    po_conflict_diff: dict[str, dict[str, Any]] | None,
    contract_conflict_diff: dict[str, dict[str, Any]] | None,
    processed_at: str,
    po_extraction_warning: str | None = None,
    contract_extraction_warning: str | None = None,
) -> PipelineResult:
    """
    Build a EXCEPTION/DOCUMENT_CONFLICT PipelineResult without running the
    full pipeline.  Called when upsert returns UpsertConflict.
    Registers the exception in the store so the dashboard and API see it.
    """
    conflict_record = ExceptionRecordCreate(
        invoice_id=invoice_id,
        reasons=[
            ExceptionReasonSchema(
                reason_code=ExceptionReasonCode.DOCUMENT_CONFLICT,
                supporting_data={
                    "po_conflict": po_conflict_diff or {},
                    "contract_conflict": contract_conflict_diff or {},
                },
            )
        ],
        status=ExceptionStatus.OPEN,
    )
    register_exception(
        ExceptionDecision(
            invoice_id=invoice_id,
            exception_record=conflict_record,
        )
    )
    return PipelineResult(
        invoice_id=invoice_id,
        invoice_number=invoice.invoice_number,
        outcome="EXCEPTION",
        exception_reasons=[ExceptionReasonCode.DOCUMENT_CONFLICT.value],
        invoice_fields=_invoice_to_dict(invoice),
        po_conflict_diff=po_conflict_diff,
        contract_conflict_diff=contract_conflict_diff,
        po_extraction_warning=po_extraction_warning,
        contract_extraction_warning=contract_extraction_warning,
        processed_at=processed_at,
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
    Run LLM extraction + full processing pipeline for a raw invoice text.

    Invoice-only variant — no PO or contract documents.  Resolves entities
    from the DB using the references embedded in the extracted invoice.

    Args:
        invoice_text: Raw UTF-8 text of the invoice document.

    Returns:
        PipelineResult.  If extraction fails, outcome='NEEDS_REEXTRACTION'.
    """
    return run_extraction_pipeline_with_documents(
        invoice_text=invoice_text,
        po_text=None,
        contract_text=None,
    )


def run_extraction_pipeline_with_documents(
    invoice_text: str,
    po_text: str | None,
    contract_text: str | None,
) -> PipelineResult:
    """
    Run LLM extraction for invoice + optional PO/contract documents, then
    process through matching and routing.

    Extraction strategy
    -------------------
    - Invoice, PO, and contract are each extracted independently.
    - PO/contract extraction failure does NOT block the invoice.  If either
      agent returns a typed ExtractionFailure (or raises an unexpected exception),
      extracted_po / extracted_contract is left as None and a human-readable
      message is captured in po_extraction_warning / contract_extraction_warning
      on the returned PipelineResult.  The matching engine will flag
      PO_NOT_FOUND / CONTRACT_NOT_FOUND as usual.

    Upsert and conflict detection
    -----------------------------
    After successful PO/contract extraction, upsert_po() / upsert_contract()
    is called within a DB session:
      - UpsertCreated / UpsertUnchanged → proceed normally into matching.
      - UpsertConflict → the invoice is immediately short-circuited to
        EXCEPTION (DOCUMENT_CONFLICT).  The existing DB row is NOT overwritten.
        A DOCUMENT_CONFLICT_DETECTED audit event is written.  The field-level
        diff is stored on PipelineResult for UI rendering.

    Args:
        invoice_text:  Raw UTF-8 text of the invoice document (required).
        po_text:       Raw text of the PO document, or None if not uploaded.
        contract_text: Raw text of the contract document, or None if not uploaded.

    Returns:
        PipelineResult.
        - outcome='NEEDS_REEXTRACTION' if invoice extraction fails.
        - outcome='EXCEPTION' with reason DOCUMENT_CONFLICT if a conflict is
          detected on an uploaded PO or contract.
        - Otherwise the normal STP/EXCEPTION routing outcome.
        - po_extraction_warning / contract_extraction_warning are non-None when
          a document was supplied but extraction failed (typed failure or raised
          exception); None when no document was uploaded or extraction succeeded.
    """
    from datetime import datetime as _dt
    settings = get_settings()
    invoice_id = str(uuid.uuid4())
    processed_at = _dt.utcnow().isoformat() + "Z"

    # -----------------------------------------------------------------------
    # 1. Extract invoice (required)
    # -----------------------------------------------------------------------
    try:
        llm_client = OpenRouterClient(settings=settings)
        invoice_agent = ExtractionAgent(llm_client=llm_client)
        invoice_extraction = invoice_agent.extract(invoice_text)
    except Exception as exc:
        return PipelineResult(
            invoice_id=invoice_id,
            invoice_number="(extraction error)",
            outcome="ERROR",
            error_message=f"LLM extraction failed: {type(exc).__name__}: {exc}",
            processed_at=processed_at,
        )

    if isinstance(invoice_extraction, ExtractionFailure):
        audit_writer.write_invoice_received(
            invoice_id=invoice_id,
            invoice_number="(unknown — extraction failed)",
        )
        audit_writer.write_extraction_failed(
            invoice_id=invoice_id,
            result=invoice_extraction,
        )
        return PipelineResult(
            invoice_id=invoice_id,
            invoice_number="(extraction failed)",
            outcome="NEEDS_REEXTRACTION",
            extraction_failure_reason=invoice_extraction.reason.value,
            processed_at=processed_at,
        )

    assert isinstance(invoice_extraction, ExtractionSuccess)
    invoice = invoice_extraction.invoice
    audit_writer.write_extraction_succeeded(invoice_id=invoice_id, result=invoice_extraction)

    # -----------------------------------------------------------------------
    # 1b. Low-confidence gate — short-circuit to EXCEPTION before any DB work
    #
    # If the LLM flagged any field as "low" confidence, route immediately to
    # EXCEPTION with reason LOW_CONFIDENCE_EXTRACTION.  The extracted values
    # ARE returned (via invoice_fields and low_confidence_fields) so the human
    # reviewer can see exactly what the LLM read and confirm or correct each
    # flagged field before the invoice can proceed to STP.
    #
    # This is additive to the existing null/missing-field behaviour:
    #   null field  → field genuinely absent from the document (no confidence flag)
    #   "low"       → field found, reading uncertain — must be human-verified
    # -----------------------------------------------------------------------
    low_confidence = _collect_low_confidence_fields(invoice_extraction, invoice)
    if low_confidence:
        lc_exception_record = ExceptionRecordCreate(
            invoice_id=invoice_id,
            reasons=[
                ExceptionReasonSchema(
                    reason_code=ExceptionReasonCode.LOW_CONFIDENCE_EXTRACTION,
                    supporting_data={
                        "low_confidence_fields": low_confidence,
                        "note": (
                            "One or more fields were extracted with low confidence. "
                            "The values shown come directly from the document but the "
                            "reading is uncertain. Please verify before approving."
                        ),
                    },
                )
            ],
            status=ExceptionStatus.OPEN,
        )
        audit_writer.write_invoice_received(
            invoice_id=invoice_id,
            invoice_number=invoice.invoice_number,
        )
        audit_writer.write_exception_raised(
            invoice_id=invoice_id,
            invoice=invoice,
            exception_record=lc_exception_record,
        )
        # Register in the exception store so the dashboard and API can see it.
        register_exception(
            ExceptionDecision(
                invoice_id=invoice_id,
                exception_record=lc_exception_record,
            )
        )
        return PipelineResult(
            invoice_id=invoice_id,
            invoice_number=invoice.invoice_number,
            outcome="EXCEPTION",
            exception_reasons=[ExceptionReasonCode.LOW_CONFIDENCE_EXTRACTION.value],
            invoice_fields=_invoice_to_dict(invoice),
            low_confidence_fields=low_confidence,
            processed_at=processed_at,
        )

    # -----------------------------------------------------------------------
    # 2. Extract PO (optional, non-blocking)
    # -----------------------------------------------------------------------
    extracted_po: PurchaseOrderCreate | None = None
    extracted_po_raw: str | None = None    # discount_term_raw equiv — not used for PO
    extracted_po_vendor_code: str | None = None  # raw vendor code for UUID resolution
    po_extraction_warning: str | None = None

    if po_text and po_text.strip():
        try:
            po_agent = PurchaseOrderExtractionAgent(llm_client=OpenRouterClient(settings=settings))
            po_extraction = po_agent.extract(po_text)
            if isinstance(po_extraction, POExtractionSuccess):
                extracted_po = po_extraction.po
                extracted_po_vendor_code = po_extraction.vendor_code_extracted
            else:
                # Typed failure — agent returned POExtractionFailure
                assert isinstance(po_extraction, POExtractionFailure)
                po_extraction_warning = (
                    f"PO extraction failed ({po_extraction.reason.value}): "
                    f"{po_extraction.error_detail or 'no detail'}"
                )
        except Exception as exc:
            po_extraction_warning = (
                f"PO extraction raised an unexpected error "
                f"({type(exc).__name__}): {exc}"
            )

    # -----------------------------------------------------------------------
    # 3. Extract contract (optional, non-blocking)
    # -----------------------------------------------------------------------
    extracted_contract: ContractCreate | None = None
    extracted_contract_raw: str | None = None
    extracted_contract_vendor_code: str | None = None  # raw vendor code for UUID resolution
    contract_extraction_warning: str | None = None

    if contract_text and contract_text.strip():
        try:
            contract_agent = ContractExtractionAgent(
                llm_client=OpenRouterClient(settings=settings)
            )
            contract_extraction = contract_agent.extract(contract_text)
            if isinstance(contract_extraction, ContractExtractionSuccess):
                extracted_contract = contract_extraction.contract
                extracted_contract_raw = contract_extraction.discount_term_raw
                extracted_contract_vendor_code = contract_extraction.vendor_code_extracted
            else:
                # Typed failure — agent returned ContractExtractionFailure
                assert isinstance(contract_extraction, ContractExtractionFailure)
                contract_extraction_warning = (
                    f"Contract extraction failed ({contract_extraction.reason.value}): "
                    f"{contract_extraction.error_detail or 'no detail'}"
                )
        except Exception as exc:
            contract_extraction_warning = (
                f"Contract extraction raised an unexpected error "
                f"({type(exc).__name__}): {exc}"
            )

    # -----------------------------------------------------------------------
    # 4. Upsert PO and contract; detect conflicts
    # -----------------------------------------------------------------------
    po_conflict_diff: dict[str, dict[str, Any]] | None = None
    contract_conflict_diff: dict[str, dict[str, Any]] | None = None

    # Resolved entities to pass to the matching engine.  These start as
    # None and are filled in from the DB after a successful upsert.
    resolved_po: PurchaseOrderCreate | None = None
    resolved_contract: ContractCreate | None = None

    # Hoisted above try so it remains accessible in the deferred-audit block
    # below (which is outside the try/except).
    _deferred_vendor_audits: list[tuple[str, _VendorResolution, str]] = []

    try:
        with get_session() as session:
            # Collect (invoice_id, vendor_resolution) pairs for audit events
            # that must be written AFTER the commit succeeds.
            _deferred_vendor_audits = []
            # Each tuple: (invoice_id, resolution, source_document_label)

            # --- PO upsert ---
            if extracted_po is not None:
                po_vendor_name = extracted_po_vendor_code or ""
                vr = _resolve_or_create_vendor(
                    session,
                    vendor_code=extracted_po_vendor_code or "",
                    vendor_name=po_vendor_name,
                    source_document="PO",
                )
                if vr.warning:
                    po_extraction_warning = po_extraction_warning or vr.warning
                    po_to_upsert = None
                else:
                    po_to_upsert = extracted_po.model_copy(
                        update={"vendor_id": vr.vendor_id}
                    )

                if po_to_upsert is not None:
                    po_result = upsert_po(session, po_to_upsert)
                    if isinstance(po_result, UpsertConflict):
                        raw_diff = _diff_to_serialisable(po_result.diff)
                        po_conflict_diff = raw_diff
                        audit_writer.write_document_conflict_detected(
                            invoice_id=invoice_id,
                            invoice=invoice,
                            document_type="PO",
                            natural_key=extracted_po.po_number,
                            diff=raw_diff,
                        )
                        # Do not commit — vendor auto-create is also rolled back
                    elif isinstance(po_result, (UpsertCreated, UpsertUnchanged)):
                        resolved_po = po_result.record
                        session.commit()  # commits vendor (if new) + PO together
                        if vr.was_created:
                            _deferred_vendor_audits.append(
                                (invoice_id, vr, "PO")
                            )

            # --- Contract upsert ---
            if extracted_contract is not None:
                contract_vendor_name = extracted_contract_vendor_code or ""
                vr = _resolve_or_create_vendor(
                    session,
                    vendor_code=extracted_contract_vendor_code or "",
                    vendor_name=contract_vendor_name,
                    source_document="Contract",
                )
                if vr.warning:
                    contract_extraction_warning = (
                        contract_extraction_warning or vr.warning
                    )
                    contract_to_upsert = None
                else:
                    contract_to_upsert = extracted_contract.model_copy(
                        update={"vendor_id": vr.vendor_id}
                    )

                if contract_to_upsert is not None:
                    contract_result = upsert_contract(
                        session,
                        contract_to_upsert,
                        discount_term_raw=extracted_contract_raw,
                    )
                    if isinstance(contract_result, UpsertConflict):
                        raw_diff = _diff_to_serialisable(contract_result.diff)
                        contract_conflict_diff = raw_diff
                        audit_writer.write_document_conflict_detected(
                            invoice_id=invoice_id,
                            invoice=invoice,
                            document_type="CONTRACT",
                            natural_key=extracted_contract.contract_reference,
                            diff=raw_diff,
                        )
                        # Do not commit — vendor auto-create is also rolled back
                    elif isinstance(contract_result, (UpsertCreated, UpsertUnchanged)):
                        resolved_contract = contract_result.record
                        session.commit()  # commits vendor (if new) + Contract together
                        if vr.was_created:
                            _deferred_vendor_audits.append(
                                (invoice_id, vr, "Contract")
                            )

    # Write deferred vendor-auto-created audit events now that the DB
    # transaction has committed successfully.  These are outside the
    # try/except so a failed audit write doesn't mask the real error.
    except Exception as exc:
        return PipelineResult(
            invoice_id=invoice_id,
            invoice_number=invoice.invoice_number,
            outcome="ERROR",
            error_message=f"DB upsert failed: {type(exc).__name__}: {exc}",
            invoice_fields=_invoice_to_dict(invoice),
            processed_at=processed_at,
        )

    # Flush deferred VENDOR_AUTO_CREATED audit events.
    # These are written only after a successful session.commit() so the
    # audit trail never claims a vendor exists that wasn't durably persisted.
    for _inv_id, _vr, _src_doc in _deferred_vendor_audits:
        audit_writer.write_vendor_auto_created(
            invoice_id=_inv_id,
            vendor_code=_vr.vendor_code,
            vendor_name=_vr.vendor_name,
            created_vendor_id=_vr.vendor_id,
            source_document=_src_doc,
        )

    # -----------------------------------------------------------------------
    # 5. Short-circuit to EXCEPTION on any conflict — do NOT overwrite DB row
    # -----------------------------------------------------------------------
    if po_conflict_diff or contract_conflict_diff:
        return _make_conflict_pipeline_result(
            invoice_id=invoice_id,
            invoice=invoice,
            po_conflict_diff=po_conflict_diff,
            contract_conflict_diff=contract_conflict_diff,
            processed_at=processed_at,
            po_extraction_warning=po_extraction_warning,
            contract_extraction_warning=contract_extraction_warning,
        )

    # -----------------------------------------------------------------------
    # 6. Entity resolution — upload-first, then DB fallback.
    #
    # Resolution order (applied independently for PO and contract):
    #   a. If a document was uploaded this run AND its upsert succeeded
    #      (UpsertCreated or UpsertUnchanged) → use the record returned by
    #      upsert_po() / upsert_contract().  This is always the canonical DB
    #      row for that natural key, populated immediately after the INSERT or
    #      confirmed as identical to an existing row.
    #   b. If no document was uploaded, or PO/contract extraction failed
    #      (non-blocking) → fall back to get_po_by_number() /
    #      get_contract_by_reference() using the reference strings embedded in
    #      the extracted invoice (invoice.po_reference /
    #      invoice.contract_reference).  This is the same lookup performed by
    #      the invoice-only pipeline path.
    #
    # Vendor is always resolved via PO.vendor_id FK (see db/resolver.py for
    # the rationale — free-text name matching is explicitly rejected).
    #
    # If neither path resolves an entity, None is passed to run_pipeline()
    # and the matching engine raises PO_NOT_FOUND / CONTRACT_NOT_FOUND as
    # usual (FR-2.6).  This preserves the pre-existing exception behaviour
    # and keeps conflict detection orthogonal to "not found" detection.
    # -----------------------------------------------------------------------
    try:
        with get_session() as session:
            db_entities = resolve_invoice_entities(session, invoice)
    except Exception as exc:
        return PipelineResult(
            invoice_id=invoice_id,
            invoice_number=invoice.invoice_number,
            outcome="ERROR",
            error_message=f"DB entity resolution failed: {type(exc).__name__}: {exc}",
            invoice_fields=_invoice_to_dict(invoice),
            processed_at=processed_at,
        )

    # Apply upload-first resolution order.
    # resolved_po / resolved_contract are non-None only when a document was
    # uploaded this run and its upsert returned Created or Unchanged.
    final_po = resolved_po if resolved_po is not None else db_entities.po
    final_contract = resolved_contract if resolved_contract is not None else db_entities.contract
    final_vendor = db_entities.vendor

    # -----------------------------------------------------------------------
    # 7. Full pipeline
    # -----------------------------------------------------------------------
    try:
        result = run_pipeline(
            invoice_id=invoice_id,
            invoice=invoice,
            vendor=final_vendor,
            po=final_po,
            contract=final_contract,
            approval_on_file=False,
        )
    except Exception as exc:
        return PipelineResult(
            invoice_id=invoice_id,
            invoice_number=invoice.invoice_number,
            outcome="ERROR",
            error_message=f"Pipeline error after extraction: {type(exc).__name__}: {exc}",
            invoice_fields=_invoice_to_dict(invoice),
            processed_at=processed_at,
        )

    return _from_service_result(
        result,
        invoice,
        invoice_id,
        po_extraction_warning=po_extraction_warning,
        contract_extraction_warning=contract_extraction_warning,
    )
