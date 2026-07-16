"""
api/invoices.py — FastAPI invoice submission and upload endpoints.

spec.md Phase 9 / requirements.md FR-1 through FR-7.

This module contains only FastAPI-specific code:
  - APIRouter, route decorators, request/response types
  - UploadFile handling, content-type validation, size limits
  - HTTPException mapping from service-layer errors

All pipeline logic lives in services/invoice_service.py.  Neither that
module nor any module it imports is a FastAPI dependency.

Endpoints:
  POST /invoices/submit   — JSON body (InvoiceSubmitRequest)
  POST /invoices/upload   — multipart/form-data (field "file")

Authorization:
  Every endpoint explicitly states its authorization requirement per spec.md §4.
  v1 auth is a documented placeholder (TBD); the contract is explicit rather
  than left implicit.

Rate limiting:
  Noted on every write endpoint per spec.md §4.  Enforcement is at the
  reverse-proxy layer in production — not implemented in-process in v1.

File validation (spec.md §4):
  - Accepted content-types: text/plain, application/pdf, application/octet-stream, text/csv.
    A generic content-type (application/octet-stream) is also accepted when the filename
    ends in .pdf — actual format is verified by extraction.document_loader.
  - Maximum file size: 1 MB (1_048_576 bytes) of raw upload bytes.
  - Text (.txt): decoded as UTF-8; decode errors raise HTTP 422.
  - PDF (.pdf): text extracted with pdfplumber; scanned/image-only PDFs and corrupt
    files raise HTTP 422 with a specific reason (not a generic decode error).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

import audit.writer as audit
from app.config import get_settings
from db.resolver import resolve_invoice_entities
from db.session import get_session
from extraction.agent import ExtractionAgent
from extraction.document_loader import DocumentLoadError, load_document_text
from extraction.llm_client import OpenRouterClient
from extraction.schemas import ExtractionFailure, ExtractionSuccess
from models.contract import ContractCreate
from models.invoice import InvoiceCreate
from models.purchase_order import PurchaseOrderCreate
from models.vendor import VendorCreate
from services.invoice_service import InvoiceProcessingResult, run_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices", tags=["invoices"])

# ---------------------------------------------------------------------------
# File-upload constants (spec.md §4)
# ---------------------------------------------------------------------------

_MAX_FILE_BYTES: int = 1_048_576  # 1 MB

_ACCEPTED_CONTENT_TYPES: frozenset[str] = frozenset([
    "text/plain",
    "application/octet-stream",
    "text/csv",
    "application/pdf",
])

# Generic binary content-types that browsers / HTTP clients sometimes send
# instead of the real MIME type.  When one of these is the declared type AND
# the filename extension is .pdf we treat the upload as a PDF rather than
# rejecting it — the actual format is verified by load_document_text().
_GENERIC_CONTENT_TYPES: frozenset[str] = frozenset([
    "application/octet-stream",
    "binary/octet-stream",
])


# ---------------------------------------------------------------------------
# HTTP request / response schemas
#
# InvoiceSubmitResponse is defined here because it is an HTTP contract —
# it describes the JSON body returned to HTTP clients.  The service layer
# returns InvoiceProcessingResult; this endpoint converts it to the HTTP shape.
# ---------------------------------------------------------------------------


class InvoiceSubmitRequest(BaseModel):
    """
    Request body for POST /invoices/submit.

    Carries a pre-extracted InvoiceCreate.  Entity resolution (vendor, PO,
    contract) is performed automatically from the database using the reference
    fields on the invoice:
      - po_reference        → PurchaseOrderORM by po_number
      - contract_reference  → ContractORM by contract_reference
      - vendor              → resolved via PO.vendor_id FK (not by name)

    The three override_* fields are provided for testing and integration
    scenarios where the caller wants to supply entities directly without a DB
    round-trip.  When supplied they take precedence over DB resolution.
    Set override_vendor=None (omit) to use DB resolution (default).
    """

    invoice: InvoiceCreate = Field(description="Pre-extracted, fully-validated invoice.")

    override_vendor: VendorCreate | None = Field(
        default=None,
        description=(
            "Optional vendor override.  When supplied, DB resolution is skipped "
            "for the vendor.  Intended for tests and integration scenarios."
        ),
    )
    override_po: PurchaseOrderCreate | None = Field(
        default=None,
        description=(
            "Optional PO override.  When supplied, DB resolution is skipped "
            "for the PO (and consequently for the vendor, unless override_vendor "
            "is also set).  Intended for tests and integration scenarios."
        ),
    )
    override_contract: ContractCreate | None = Field(
        default=None,
        description=(
            "Optional contract override.  When supplied, DB resolution is skipped "
            "for the contract.  Intended for tests and integration scenarios."
        ),
    )
    approval_on_file: bool = Field(
        default=False,
        description="Whether a manual approval is on file for this invoice.",
    )
    invoice_id: str | None = Field(
        default=None,
        description=(
            "Stable identifier for this invoice.  Auto-generated (UUID4) if not "
            "supplied.  Reusing an ID re-processes the invoice (idempotent)."
        ),
    )


class InvoiceSubmitResponse(BaseModel):
    """HTTP response body for both submission endpoints."""

    invoice_id: str
    invoice_number: str
    outcome: str = Field(
        description="'STP' | 'EXCEPTION' | 'NEEDS_REEXTRACTION'"
    )
    payment_schedule: dict[str, Any] | None = None
    exception_reasons: list[str] | None = None
    discount_recommendation: str | None = None
    extraction_failure_reason: str | None = None
    processed_at: str


def _to_http_response(result: InvoiceProcessingResult) -> InvoiceSubmitResponse:
    """Convert the framework-agnostic service result to the HTTP response shape."""
    return InvoiceSubmitResponse(
        invoice_id=result.invoice_id,
        invoice_number=result.invoice_number,
        outcome=result.outcome,
        payment_schedule=result.payment_schedule,
        exception_reasons=result.exception_reasons or None,
        discount_recommendation=result.discount_recommendation,
        extraction_failure_reason=result.extraction_failure_reason,
        processed_at=result.processed_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/submit",
    status_code=status.HTTP_201_CREATED,
    summary="Submit a pre-extracted invoice for processing",
    description=(
        "Accepts a fully-validated `InvoiceCreate` in the request body and "
        "runs the complete downstream pipeline: matching → routing → audit → "
        "payment scheduling (if STP) → discount evaluation (if STP, FR-7).\n\n"
        "Use this endpoint when the caller has already extracted the invoice "
        "fields (e.g. from a system integration).  For raw document upload use "
        "`POST /invoices/upload` instead.\n\n"
        "**Idempotency:** supplying the same `invoice_id` twice re-processes "
        "the invoice and overwrites the prior payment schedule / exception record "
        "(idempotent re-processing per spec.md §3).  The audit trail accumulates "
        "a second chain — this is correct (reprocessing must be visible).\n\n"
        "**Rate limiting:** recommended 60 req/min per client IP at the proxy "
        "layer (spec.md §4).  No enforcement in-process in v1.\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required.  "
        "Pass in `Authorization: Bearer <token>`."
    ),
    response_model=InvoiceSubmitResponse,
)
def submit_invoice(body: InvoiceSubmitRequest) -> InvoiceSubmitResponse:
    """
    Submit a pre-extracted invoice for matching, routing, and scheduling.

    Authorization: internal service token (TBD — see spec.md §4 / Phase 9).
    Rate limiting: 60 req/min per client IP at proxy (spec.md §4).

    Raises:
        422: InvoiceCreate body fails Pydantic validation.
        500: unexpected pipeline error.
    """
    invoice_id = body.invoice_id or str(uuid.uuid4())

    # -----------------------------------------------------------------------
    # Entity resolution — DB first, override fields take precedence.
    #
    # Resolution strategy (see db/resolver.py):
    #   PO by invoice.po_reference → vendor by PO.vendor_id FK
    #   Contract by invoice.contract_reference
    #
    # If override_* fields are supplied they bypass DB resolution for that
    # entity.  This is intentional: tests and integration callers can supply
    # known-good entities directly without a running database.
    # -----------------------------------------------------------------------
    try:
        if body.override_po is None or body.override_contract is None:
            with get_session() as session:
                entities = resolve_invoice_entities(session, body.invoice)
        else:
            # All three overrides supplied — skip the DB entirely.
            from db.resolver import ResolvedEntities
            entities = ResolvedEntities(
                vendor=body.override_vendor,
                po=body.override_po,
                contract=body.override_contract,
            )
    except Exception as exc:
        logger.error(
            "DB entity resolution failed in /invoices/submit",
            extra={"invoice_id": invoice_id, "error": str(exc)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Entity resolution failed for invoice_id={invoice_id!r}: {exc!s}"
            ),
        ) from exc

    # Allow individual override fields to replace DB-resolved entities.
    vendor = body.override_vendor if body.override_vendor is not None else entities.vendor
    po     = body.override_po     if body.override_po     is not None else entities.po
    contract = body.override_contract if body.override_contract is not None else entities.contract

    try:
        result = run_pipeline(
            invoice_id=invoice_id,
            invoice=body.invoice,
            vendor=vendor,
            po=po,
            contract=contract,
            approval_on_file=body.approval_on_file,
        )
    except Exception as exc:
        logger.error(
            "Unexpected error in invoice submit pipeline",
            extra={"invoice_id": invoice_id, "error": str(exc)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Unexpected pipeline error for invoice_id={invoice_id!r}.  "
                "The invoice was not scheduled for payment.  "
                f"Details: {exc!s}"
            ),
        ) from exc

    return _to_http_response(result)


@router.post(
    "/upload",
    status_code=status.HTTP_201_CREATED,
    summary="Upload a raw invoice document for extraction and processing",
    description=(
        "Accepts a plain-text or PDF invoice document (max 1 MB raw) via "
        "multipart/form-data (field name: `file`).  Runs LLM extraction "
        "(spec.md §1 model-portability rule), then routes the extracted "
        "invoice through matching → routing → audit → payment scheduling → "
        "discount evaluation.\n\n"
        "If extraction fails, `outcome='NEEDS_REEXTRACTION'` with "
        "`extraction_failure_reason` populated (FR-5.1).\n\n"
        "**File validation (spec.md §4):**\n"
        "- Accepted: text/plain, application/pdf, application/octet-stream, text/csv.\n"
        "  A generic content-type (application/octet-stream) is also accepted when the "
        "  filename ends in `.pdf`.\n"
        "- Max size: 1 MB (raw upload bytes).\n"
        "- `.txt` files must be valid UTF-8.\n"
        "- `.pdf` files must have an embedded text layer; scanned/image PDFs → HTTP 422.\n\n"
        "**Rate limiting:** 30 uploads/min per client IP at proxy (spec.md §4).\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required."
    ),
    response_model=InvoiceSubmitResponse,
)
async def upload_invoice(
    file: UploadFile = File(
        ...,
        description="Plain-text (.txt, UTF-8) or PDF (.pdf, text layer required) invoice document, max 1 MB.",
    ),
) -> InvoiceSubmitResponse:
    """
    Upload a raw invoice document for extraction and pipeline processing.

    Authorization: internal service token (TBD — see spec.md §4 / Phase 9).
    Rate limiting: 30 uploads/min per client IP at proxy (spec.md §4).

    File validation:
        - Accepted content-types: text/plain, application/pdf,
          application/octet-stream, text/csv.  A generic content-type
          (application/octet-stream) is also accepted when the filename
          ends in .pdf.
        - Max raw upload size: 1 MB (1_048_576 bytes).
        - Text files decoded as UTF-8; PDF files require an embedded text
          layer (scanned/image-only PDFs are rejected with a specific reason).

    Raises:
        413: file exceeds 1 MB.
        415: unsupported content type (not text/plain, application/pdf,
             application/octet-stream, or text/csv).
        422: document cannot be loaded — invalid UTF-8, scanned/image PDF,
             corrupt PDF, or unsupported file type.
        500: unexpected pipeline error.
    """
    # -----------------------------------------------------------------------
    # File validation — spec.md §4
    # -----------------------------------------------------------------------
    content_type = (file.content_type or "application/octet-stream").split(";")[0].strip()
    filename = file.filename or ""
    filename_lower = filename.lower()

    # Determine whether to accept this upload.
    #
    # Rule 1 — explicit PDF content-type is accepted directly.
    # Rule 2 — generic binary content-type is accepted when the filename
    #           ends in .pdf: some HTTP clients (curl --data-binary, certain
    #           proxies) send application/octet-stream regardless of extension.
    #           load_document_text() will verify the actual format.
    # Rule 3 — everything else must be in _ACCEPTED_CONTENT_TYPES.
    is_pdf_by_extension = (
        content_type in _GENERIC_CONTENT_TYPES and filename_lower.endswith(".pdf")
    )
    if content_type not in _ACCEPTED_CONTENT_TYPES and not is_pdf_by_extension:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported content type {content_type!r}.  "
                f"Accepted: {sorted(_ACCEPTED_CONTENT_TYPES)}."
            ),
        )

    raw_bytes = await file.read(_MAX_FILE_BYTES + 1)
    if len(raw_bytes) > _MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {_MAX_FILE_BYTES:,} bytes (1 MB).",
        )

    # Resolve the effective content-type to pass to load_document_text().
    # When a generic type was accepted because the filename ends in .pdf,
    # override to application/pdf so load_document_text() uses the PDF path.
    effective_content_type = (
        "application/pdf" if is_pdf_by_extension else content_type
    )

    try:
        invoice_text = load_document_text(
            raw_bytes,
            filename=filename,
            content_type=effective_content_type,
        )
    except DocumentLoadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.message,
        ) from exc

    invoice_id = str(uuid.uuid4())
    logger.info(
        "Invoice upload received",
        extra={
            "invoice_id": invoice_id,
            "upload_filename": file.filename,
            "content_type": content_type,
            "size_bytes": len(raw_bytes),
        },
    )

    # -----------------------------------------------------------------------
    # Extraction — LLM with retry-and-repair loop (spec.md §1)
    # -----------------------------------------------------------------------
    settings = get_settings()
    llm_client = OpenRouterClient(settings=settings)
    agent = ExtractionAgent(llm_client=llm_client)
    extraction_result = agent.extract(invoice_text)

    if isinstance(extraction_result, ExtractionFailure):
        audit.write_invoice_received(
            invoice_id=invoice_id,
            invoice_number="(unknown — extraction failed)",
            source_file_ref=file.filename,
        )
        audit.write_extraction_failed(
            invoice_id=invoice_id,
            result=extraction_result,
            invoice_number=None,
        )
        logger.warning(
            "Invoice rejected: NEEDS_REEXTRACTION",
            extra={
                "invoice_id": invoice_id,
                "reason": extraction_result.reason.value,
            },
        )
        return InvoiceSubmitResponse(
            invoice_id=invoice_id,
            invoice_number="(extraction failed)",
            outcome="NEEDS_REEXTRACTION",
            extraction_failure_reason=extraction_result.reason.value,
            processed_at=datetime.utcnow().isoformat() + "Z",
        )

    assert isinstance(extraction_result, ExtractionSuccess)
    audit.write_extraction_succeeded(invoice_id=invoice_id, result=extraction_result)

    # -----------------------------------------------------------------------
    # Entity resolution — look up vendor, PO, and contract from the DB.
    #
    # Resolution strategy (mirrors submit_invoice and pipeline_runner):
    #   PO by invoice.po_reference → vendor by PO.vendor_id FK
    #   Contract by invoice.contract_reference
    #
    # Any entity that cannot be resolved is None; the matching engine will
    # raise the appropriate exception reason code rather than crashing.
    # -----------------------------------------------------------------------
    try:
        with get_session() as session:
            entities = resolve_invoice_entities(session, extraction_result.invoice)
    except Exception as exc:
        logger.error(
            "DB entity resolution failed in /invoices/upload",
            extra={"invoice_id": invoice_id, "error": str(exc)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Entity resolution failed for invoice_id={invoice_id!r}: {exc!s}"
            ),
        ) from exc
    try:
        result = run_pipeline(
            invoice_id=invoice_id,
            invoice=extraction_result.invoice,
            vendor=entities.vendor,
            po=entities.po,
            contract=entities.contract,
            approval_on_file=False,
        )
    except Exception as exc:
        logger.error(
            "Unexpected error in invoice upload pipeline",
            extra={"invoice_id": invoice_id, "error": str(exc)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Unexpected pipeline error for invoice_id={invoice_id!r}.  "
                f"Details: {exc!s}"
            ),
        ) from exc

    return _to_http_response(result)
