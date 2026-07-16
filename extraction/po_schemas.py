"""
extraction/po_schemas.py — Typed result types for Purchase Order extraction.

Mirrors the structure of extraction/schemas.py (invoice extraction) but
wraps PurchaseOrderCreate instead of InvoiceCreate.

Design decisions
----------------
PurchaseOrderCreate.vendor_id is a UUID FK that cannot be determined by
extracting a PO document — the document contains a vendor name or vendor code,
not the DB primary key.  Two approaches are possible:

  Option A: populate vendor_id with a sentinel/placeholder and require the
            caller to overwrite it after vendor resolution.
  Option B: surface the extracted vendor identifier in a separate field on
            the success type so callers have it explicitly.

This module uses Option B.  POExtractionSuccess carries:

  - po: PurchaseOrderCreate — the fully-validated model.  vendor_id is
        populated with the value from vendor_code_extracted (a string the
        LLM read from the document).  Callers MUST replace po.vendor_id
        with the real UUID after resolving the vendor from the DB.
  - vendor_code_extracted: str — the vendor code or name as extracted from
        the source document.  Used by the caller to look up the vendor record.

The PO extraction prompt (v1_extract_po.md) asks the LLM to extract
vendor_code into a top-level field; the caller passes that value through
as vendor_id when constructing PurchaseOrderCreate (allowing Pydantic
validation to pass), then overwrites it after DB resolution.

FailureReason and the result Union are re-exported from extraction.schemas —
the failure modes are document-type-agnostic and do not need separate enums.

Public API
----------
POExtractionSuccess  — successful PO extraction
POExtractionFailure  — failed extraction (NEEDS_REEXTRACTION)
POExtractionResult   — Union[POExtractionSuccess, POExtractionFailure]
"""

from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field

from models.enums import ExtractionStatus
from models.purchase_order import PurchaseOrderCreate

# Re-export FailureReason — same codes apply for any document type.
from extraction.schemas import ConfidenceLevel, FailureReason

__all__ = [
    "ConfidenceLevel",
    "FailureReason",
    "POExtractionFailure",
    "POExtractionResult",
    "POExtractionSuccess",
]


class POExtractionSuccess(BaseModel):
    """
    Successful PO extraction — carries a fully-validated PurchaseOrderCreate.

    Attributes:
        po: Validated PurchaseOrderCreate.  Note that po.vendor_id is
            populated with the raw value extracted from the document
            (vendor_code_extracted).  Callers must replace it with the
            real DB UUID after resolving the vendor.

        vendor_code_extracted: The vendor code or name as read from the
            source document.  Used to look up VendorORM by vendor_code.
            Stored separately so the caller never has to parse po.vendor_id
            to distinguish "real UUID" from "extracted placeholder".

        raw_payload: Raw JSON string returned by the LLM, stored verbatim
            for audit (FR-6.1).  Never modified after construction.

        attempt_count: 1 if the first LLM call produced valid JSON,
            2 if a retry was required (spec.md §1 model-portability rule).
    """

    outcome: Literal["success"] = "success"
    extraction_status: ExtractionStatus = ExtractionStatus.EXTRACTED

    po: PurchaseOrderCreate = Field(
        description=(
            "Validated PurchaseOrderCreate.  po.vendor_id is initially set to "
            "vendor_code_extracted; callers must replace it with the real DB UUID."
        )
    )
    vendor_code_extracted: str = Field(
        min_length=1,
        description=(
            "Vendor code or name as extracted from the source document.  "
            "Used to resolve the vendor UUID from the database."
        ),
    )
    raw_payload: str = Field(
        description="Raw JSON string returned by the LLM (FR-6.1 audit trail)."
    )
    attempt_count: int = Field(
        default=1,
        ge=1,
        le=2,
        description="1 = first attempt succeeded; 2 = succeeded after retry.",
    )
    field_confidence: dict[str, ConfidenceLevel] = Field(
        default_factory=dict,
        description=(
            "Per-field confidence reported by the LLM.  Keys are field names "
            "(e.g. 'po_total', 'unit_price').  A missing key means the LLM "
            "did not flag that field — treat as implicitly 'high'.  "
            "'low' means the value was present but the reading was uncertain; "
            "the value still comes from the document, never invented."
        ),
    )


class POExtractionFailure(BaseModel):
    """
    Failed PO extraction — document flagged NEEDS_REEXTRACTION.

    Structurally identical to ExtractionFailure in extraction/schemas.py;
    kept as a separate class so type-checkers can distinguish PO failures
    from invoice failures without inspecting payload contents.
    """

    outcome: Literal["failure"] = "failure"
    extraction_status: ExtractionStatus = ExtractionStatus.NEEDS_REEXTRACTION
    reason: FailureReason
    error_detail: str = Field(
        description="Human-readable description of why PO extraction failed."
    )
    raw_payload: str | None = Field(
        default=None,
        description="Raw LLM response (if any) for audit purposes.",
    )
    attempt_count: int = Field(
        default=1,
        ge=1,
        le=2,
    )


# Union used as the return type of any PO extraction agent.
POExtractionResult = Union[POExtractionSuccess, POExtractionFailure]
