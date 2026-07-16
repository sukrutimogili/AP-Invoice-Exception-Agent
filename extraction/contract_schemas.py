"""
extraction/contract_schemas.py — Typed result types for Contract extraction.

Mirrors the structure of extraction/schemas.py (invoice extraction) but
wraps ContractCreate instead of InvoiceCreate.

Design decisions
----------------
Two fields on ContractCreate require special handling during LLM extraction:

1.  vendor_id (UUID FK)
    Same problem as PO extraction — the document contains a vendor code or
    name, not a DB primary key.  ContractExtractionSuccess carries a separate
    vendor_code_extracted field.  Callers populate contract.vendor_id with
    the extracted string initially (satisfying Pydantic's non-empty validation)
    then replace it with the real UUID after DB resolution.

2.  discount_term (DiscountTermSchema | None)
    DiscountTermSchema requires discount_pct, discount_days, and net_days as
    validated numeric fields.  LLMs can reliably extract the raw term string
    (e.g. "2/10 net 30"), but parsing it into structured numerics is the job
    of discount/parser.py::parse_discount_term() — which already handles both
    the canonical regex path and an LLM fallback for non-standard prose.

    Therefore:
      - The extraction prompt (v1_extract_contract.md) asks the LLM to copy
        the exact term string into discount_term_raw (a plain string field).
      - ContractExtractionSuccess exposes that raw string as
        discount_term_raw: str | None.
      - contract.discount_term is set to None at extraction time.
      - Callers invoke parse_discount_term(discount_term_raw) to populate the
        parsed DiscountTermSchema and pass it into the matching/discount layers.

    This separation keeps the extraction agent responsible for reading,
    parse_discount_term() responsible for interpreting, and avoids asking the
    LLM to produce validated numeric fractions (a common LLM error source).

FailureReason is re-exported from extraction.schemas — the failure modes are
document-type-agnostic and do not need separate enums.

Public API
----------
ContractExtractionSuccess  — successful contract extraction
ContractExtractionFailure  — failed extraction (NEEDS_REEXTRACTION)
ContractExtractionResult   — Union[ContractExtractionSuccess, ContractExtractionFailure]
"""

from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field

from models.enums import ExtractionStatus
from models.contract import ContractCreate

# Re-export FailureReason — same codes apply for any document type.
from extraction.schemas import ConfidenceLevel, FailureReason

__all__ = [
    "ConfidenceLevel",
    "ContractExtractionFailure",
    "ContractExtractionResult",
    "ContractExtractionSuccess",
    "FailureReason",
]


class ContractExtractionSuccess(BaseModel):
    """
    Successful contract extraction — carries a fully-validated ContractCreate.

    Attributes:
        contract: Validated ContractCreate.
            - contract.vendor_id is populated with vendor_code_extracted
              (a placeholder); callers must replace it with the real DB UUID.
            - contract.discount_term is None at extraction time; callers must
              call discount.parser.parse_discount_term(discount_term_raw) to
              obtain the parsed DiscountTermSchema when needed.

        vendor_code_extracted: Vendor code or name as read from the source
            document.  Used to resolve the vendor UUID from the database.
            Stored separately from contract.vendor_id so callers have an
            unambiguous signal that the FK has not yet been resolved.

        discount_term_raw: The exact discount term string copied verbatim from
            the contract document (e.g. "2/10 net 30", "1.5/15 net 45"), or
            None if no early-payment discount term is present.
            Pass this value to discount.parser.parse_discount_term() to obtain
            a validated DiscountTermSchema for use in the discount evaluator
            (FR-7.1).  Never parse or interpret this string here.

        raw_payload: Raw JSON string returned by the LLM, stored verbatim
            for audit (FR-6.1).  Never modified after construction.

        attempt_count: 1 if the first LLM call produced valid JSON,
            2 if a retry was required (spec.md §1 model-portability rule).
    """

    outcome: Literal["success"] = "success"
    extraction_status: ExtractionStatus = ExtractionStatus.EXTRACTED

    contract: ContractCreate = Field(
        description=(
            "Validated ContractCreate.  contract.vendor_id is initially set to "
            "vendor_code_extracted; callers must replace it with the real DB UUID.  "
            "contract.discount_term is None — call parse_discount_term(discount_term_raw) "
            "to obtain the parsed DiscountTermSchema."
        )
    )
    vendor_code_extracted: str = Field(
        min_length=1,
        description=(
            "Vendor code or name as extracted from the source document.  "
            "Used to resolve the vendor UUID from the database."
        ),
    )
    discount_term_raw: str | None = Field(
        default=None,
        description=(
            "Verbatim early-payment discount term string extracted from the document "
            "(e.g. '2/10 net 30').  None if no discount term is present.  "
            "Pass to discount.parser.parse_discount_term() for numeric parsing."
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
            "(e.g. 'unit_price', 'approval_threshold').  A missing key means the "
            "LLM did not flag that field — treat as implicitly 'high'.  "
            "'low' means the value was present but the reading was uncertain; "
            "the value still comes from the document, never invented."
        ),
    )


class ContractExtractionFailure(BaseModel):
    """
    Failed contract extraction — document flagged NEEDS_REEXTRACTION.

    Structurally identical to ExtractionFailure in extraction/schemas.py;
    kept as a separate class so type-checkers can distinguish contract
    failures from invoice failures without inspecting payload contents.
    """

    outcome: Literal["failure"] = "failure"
    extraction_status: ExtractionStatus = ExtractionStatus.NEEDS_REEXTRACTION
    reason: FailureReason
    error_detail: str = Field(
        description="Human-readable description of why contract extraction failed."
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


# Union used as the return type of any contract extraction agent.
ContractExtractionResult = Union[ContractExtractionSuccess, ContractExtractionFailure]
