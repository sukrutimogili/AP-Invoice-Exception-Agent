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
  - Accepted content-types: text/plain, application/octet-stream, text/csv.
  - Maximum file size: 1 MB (1_048_576 bytes).
  - Content decoded as UTF-8; decode errors raise HTTP 422.
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
from extraction.agent import ExtractionAgent
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

    Carries a pre-extracted InvoiceCreate alongside optional mock context
    (PO, contract, vendor) for Phase 9 in-process resolution.

    TBD: replace mock_* fields with DB resolution via FastAPI Depends once
         a DB session is wired in — see spec.md §3 (Dependency injection).
    """

    invoice: InvoiceCreate = Field(description="Pre-extracted, fully-validated invoice.")

    mock_vendor: VendorCreate | None = Field(
        default=None,
        description=(
            "Mock vendor record.  If omitted the engine raises UNKNOWN_VENDOR.  "
            "Replace with DB lookup in production."
        ),
    )
    mock_po: PurchaseOrderCreate | None = Field(
        default=None,
        description=(
            "Mock purchase order.  If omitted the engine raises PO_NOT_FOUND.  "
            "Replace with DB lookup in production."
        ),
    )
    mock_contract: ContractCreate | None = Field(
        default=None,
        description=(
            "Mock contract.  If omitted the engine raises CONTRACT_NOT_FOUND.  "
            "Replace with DB lookup in production."
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

    try:
        result = run_pipeline(
            invoice_id=invoice_id,
            invoice=body.invoice,
            vendor=body.mock_vendor,
            po=body.mock_po,
            contract=body.mock_contract,
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
        "Accepts a plain-text invoice document (max 1 MB, UTF-8) via "
        "multipart/form-data (field name: `file`).  Runs LLM extraction "
        "(spec.md §1 model-portability rule), then routes the extracted "
        "invoice through matching → routing → audit → payment scheduling → "
        "discount evaluation.\n\n"
        "If extraction fails, `outcome='NEEDS_REEXTRACTION'` with "
        "`extraction_failure_reason` populated (FR-5.1).\n\n"
        "**File validation (spec.md §4):**\n"
        "- Accepted: text/plain, application/octet-stream, text/csv.\n"
        "- Max size: 1 MB.\n"
        "- Must be valid UTF-8.\n\n"
        "**Rate limiting:** 30 uploads/min per client IP at proxy (spec.md §4).\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required."
    ),
    response_model=InvoiceSubmitResponse,
)
async def upload_invoice(
    file: UploadFile = File(
        ...,
        description="Plain-text invoice document (UTF-8, max 1 MB).",
    ),
) -> InvoiceSubmitResponse:
    """
    Upload a raw invoice document for extraction and pipeline processing.

    Authorization: internal service token (TBD — see spec.md §4 / Phase 9).
    Rate limiting: 30 uploads/min per client IP at proxy (spec.md §4).

    Raises:
        413: file exceeds 1 MB.
        415: unsupported content type.
        422: file is not valid UTF-8.
        500: unexpected pipeline error.
    """
    # -----------------------------------------------------------------------
    # File validation — spec.md §4
    # -----------------------------------------------------------------------
    content_type = (file.content_type or "application/octet-stream").split(";")[0].strip()
    if content_type not in _ACCEPTED_CONTENT_TYPES:
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

    try:
        invoice_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File is not valid UTF-8: {exc!s}",
        ) from exc

    invoice_id = str(uuid.uuid4())
    logger.info(
        "Invoice upload received",
        extra={
            "invoice_id": invoice_id,
            "filename": file.filename,
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
    # Downstream pipeline — delegates to the service layer
    #
    # TBD: resolve vendor/PO/contract from DB via FastAPI Depends.
    # -----------------------------------------------------------------------
    try:
        result = run_pipeline(
            invoice_id=invoice_id,
            invoice=extraction_result.invoice,
            vendor=None,    # TBD: resolve from DB by invoice.vendor_name
            po=None,        # TBD: resolve from DB by invoice.po_reference
            contract=None,  # TBD: resolve from DB by invoice.contract_reference
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
