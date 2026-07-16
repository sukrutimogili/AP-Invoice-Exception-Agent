"""
models/enums.py — All enumeration types used across domain models.

Every enum is defined here and imported by the individual model files so
there is one canonical definition and no circular imports.

Sources:
  requirements.md FR-4.2  → ExceptionReasonCode
  requirements.md FR-5.1  → ExtractionStatus (NEEDS_REEXTRACTION)
  requirements.md FR-3    → InvoiceStatus (STP path)
  requirements.md FR-4.3  → ExceptionStatus / HumanAction
  requirements.md FR-7    → DiscountRecommendationOutcome, DiscountWindowStatus
  requirements.md FR-6    → AuditEventType
"""

from __future__ import annotations

import enum


class ExtractionStatus(str, enum.Enum):
    """
    Lifecycle status of an invoice's extraction phase (FR-1, FR-5.1).

    PENDING           — received, extraction not yet attempted.
    EXTRACTED         — LLM returned output; Pydantic validation passed.
    NEEDS_REEXTRACTION — required field missing or schema validation failed;
                         invoice is rejected before matching runs (FR-5.1).
    """

    PENDING = "PENDING"
    EXTRACTED = "EXTRACTED"
    NEEDS_REEXTRACTION = "NEEDS_REEXTRACTION"


class InvoiceStatus(str, enum.Enum):
    """
    Overall lifecycle status of an invoice through the processing pipeline.

    RECEIVED    — uploaded, awaiting extraction.
    EXTRACTED   — extraction validated; awaiting matching.
    MATCHED     — all FR-3.1 checks passed; eligible for STP.
    STP         — straight-through-processed; payment scheduled (FR-3.2).
    EXCEPTION   — one or more FR-3.1 checks failed; routed to human (FR-4.1).
    REJECTED    — malformed extraction; never entered matching (FR-5.1).
    APPROVED    — human approved an exception invoice (FR-4.3).
    DENIED      — human rejected the invoice back to vendor (FR-4.3).
    """

    RECEIVED = "RECEIVED"
    EXTRACTED = "EXTRACTED"
    MATCHED = "MATCHED"
    STP = "STP"
    EXCEPTION = "EXCEPTION"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    DENIED = "DENIED"


class ExceptionReasonCode(str, enum.Enum):
    """
    Structured reason codes for exception routing (FR-4.2).

    Each code maps to one failing FR-3.1 sub-condition.
    """

    PRICE_VARIANCE = "PRICE_VARIANCE"
    QTY_MISMATCH = "QTY_MISMATCH"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    MISSING_APPROVAL = "MISSING_APPROVAL"
    OFF_CONTRACT_TERM = "OFF_CONTRACT_TERM"
    UNKNOWN_VENDOR = "UNKNOWN_VENDOR"
    PO_NOT_FOUND = "PO_NOT_FOUND"
    CONTRACT_NOT_FOUND = "CONTRACT_NOT_FOUND"
    NEEDS_REEXTRACTION = "NEEDS_REEXTRACTION"
    DOCUMENT_CONFLICT = "DOCUMENT_CONFLICT"
    LOW_CONFIDENCE_EXTRACTION = "LOW_CONFIDENCE_EXTRACTION"


class ExceptionStatus(str, enum.Enum):
    """
    Resolution status of an ExceptionRecord (FR-4.3).

    OPEN      — awaiting human review.
    RESOLVED  — human has taken an action (approved or rejected).
    """

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class HumanAction(str, enum.Enum):
    """
    The action taken by a human reviewer on an exception (FR-4.3).

    APPROVE_OVERRIDE — invoice approved despite exception; audit records
                       the override with attribution and reason.
    REJECT           — invoice sent back to vendor.
    """

    APPROVE_OVERRIDE = "APPROVE_OVERRIDE"
    REJECT = "REJECT"


class DiscountRecommendation(str, enum.Enum):
    """
    The system's recommendation on whether to take an early-payment discount
    (FR-7.2).

    TAKE_DISCOUNT  — annualized return ≥ hurdle rate; recommend early payment.
    HOLD_TO_NET    — annualized return < hurdle rate; hold to standard terms.
    WINDOW_MISSED  — discount window has already lapsed (FR-7.5).
    NO_DISCOUNT    — contract has no discount term.
    """

    TAKE_DISCOUNT = "TAKE_DISCOUNT"
    HOLD_TO_NET = "HOLD_TO_NET"
    WINDOW_MISSED = "WINDOW_MISSED"
    NO_DISCOUNT = "NO_DISCOUNT"


class AuditEventType(str, enum.Enum):
    """
    State-transition labels written to the append-only audit log (FR-6.1).

    One event per state transition; the sequence reconstructs full history.
    """

    INVOICE_RECEIVED = "INVOICE_RECEIVED"
    EXTRACTION_SUCCEEDED = "EXTRACTION_SUCCEEDED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    MATCHING_COMPLETED = "MATCHING_COMPLETED"
    STP_APPROVED = "STP_APPROVED"
    EXCEPTION_RAISED = "EXCEPTION_RAISED"
    HUMAN_OVERRIDE_APPROVED = "HUMAN_OVERRIDE_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    PAYMENT_SCHEDULED = "PAYMENT_SCHEDULED"
    DISCOUNT_EVALUATED = "DISCOUNT_EVALUATED"
    DOCUMENT_CONFLICT_DETECTED = "DOCUMENT_CONFLICT_DETECTED"
    VENDOR_AUTO_CREATED = "VENDOR_AUTO_CREATED"
    INVESTIGATION_COMPLETED = "INVESTIGATION_COMPLETED"
    VENDOR_RISK_FLAGGED = "VENDOR_RISK_FLAGGED"
