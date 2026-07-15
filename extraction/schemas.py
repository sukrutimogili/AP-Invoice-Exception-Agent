"""
extraction/schemas.py — Typed output types for the extraction agent.

The agent always returns an ExtractionResult — either a success carrying a
validated InvoiceCreate, or a failure carrying a reason and the raw payload.

Using a typed union means callers never receive an ambiguous None or dict —
every code path is explicit and checked by mypy.

spec.md §1 model-portability rule:
  On second validation failure → fail closed to NEEDS_REEXTRACTION.
  Never fabricate a missing field.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Union

from pydantic import BaseModel, Field

from models.enums import ExtractionStatus
from models.invoice import InvoiceCreate


class FailureReason(str, Enum):
    """Reason codes for extraction failure (FR-1.3, FR-5.1)."""

    # LLM returned text that could not be parsed as JSON at all.
    UNPARSEABLE_JSON = "UNPARSEABLE_JSON"
    # JSON parsed but failed Pydantic schema validation.
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    # Retry also failed validation.
    RETRY_VALIDATION_FAILED = "RETRY_VALIDATION_FAILED"
    # LLM call failed (network error, rate limit, etc.).
    LLM_CALL_FAILED = "LLM_CALL_FAILED"
    # Required field is null/missing in the extracted JSON.
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"


class ExtractionSuccess(BaseModel):
    """Successful extraction — carries a fully-validated InvoiceCreate."""

    outcome: Literal["success"] = "success"
    extraction_status: ExtractionStatus = ExtractionStatus.EXTRACTED
    invoice: InvoiceCreate
    raw_payload: str = Field(
        description="Raw JSON string returned by the LLM, stored for audit (FR-6.1)."
    )
    attempt_count: int = Field(
        default=1,
        ge=1,
        le=2,
        description="1 = succeeded on first attempt; 2 = succeeded after retry.",
    )


class ExtractionFailure(BaseModel):
    """
    Failed extraction — invoice flagged NEEDS_REEXTRACTION (FR-5.1).

    Never enters the payment path. Carries the reason and raw payload for audit.
    """

    outcome: Literal["failure"] = "failure"
    extraction_status: ExtractionStatus = ExtractionStatus.NEEDS_REEXTRACTION
    reason: FailureReason
    error_detail: str = Field(
        description="Human-readable description of why extraction failed."
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


# Union type used as the return type of ExtractionAgent.extract()
ExtractionResult = Union[ExtractionSuccess, ExtractionFailure]
