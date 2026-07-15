"""
models/invoice.py — Invoice + InvoiceLineItem domain models.

Requirements.md §6: 'Invoice — raw + extracted fields, extraction status,
source file reference.'
FR-1.1: required extraction fields: vendor_name, invoice_number, invoice_date,
        po_reference, contract_reference, line_items (description, qty,
        unit_price, amount), subtotal, tax, grand_total, due_date,
        payment_terms.
FR-1.2: output must validate against this strict Pydantic schema.
FR-1.3: missing required field → NEEDS_REEXTRACTION; never fabricate.
FR-1.4: free-text fields are untrusted data (documented in field docstrings).

Pydantic schemas  → InvoiceLineItemBase, InvoiceBase, InvoiceCreate,
                    InvoiceRead, InvoiceExtracted
SQLAlchemy tables → InvoiceLineItemORM, InvoiceORM
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin
from models.enums import ExtractionStatus, InvoiceStatus


# ---------------------------------------------------------------------------
# SQLAlchemy ORM tables
# ---------------------------------------------------------------------------


class InvoiceLineItemORM(Base, TimestampMixin):
    """A single extracted line from an invoice."""

    __tablename__ = "invoice_line_items"

    invoice_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Free-text — treated as untrusted data (FR-1.4 / spec.md §4).
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    qty: Mapped[str] = mapped_column(Numeric(18, 4), nullable=False)
    unit_price: Mapped[str] = mapped_column(Numeric(18, 4), nullable=False)
    amount: Mapped[str] = mapped_column(
        Numeric(18, 2),
        nullable=False,
        doc="Line total as stated on the invoice (qty × unit_price).",
    )

    invoice: Mapped["InvoiceORM"] = relationship("InvoiceORM", back_populates="line_items")


class InvoiceORM(Base, TimestampMixin):
    """Invoice header — raw + extracted fields."""

    __tablename__ = "invoices"

    # ---- Identity / source ---------------------------------------------------
    invoice_number: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        doc="Invoice number as extracted from the document.",
    )
    source_file_ref: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        doc="Path or storage key for the original uploaded file.",
    )

    # ---- Status --------------------------------------------------------------
    extraction_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ExtractionStatus.PENDING.value,
    )
    invoice_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=InvoiceStatus.RECEIVED.value,
    )

    # ---- Extracted fields (all nullable until extraction succeeds) -----------
    vendor_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc="Extracted vendor name. Untrusted input (FR-1.4).",
    )
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    po_reference: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    contract_reference: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    subtotal: Mapped[str | None] = mapped_column(Numeric(18, 2), nullable=True)
    tax: Mapped[str | None] = mapped_column(Numeric(18, 2), nullable=True)
    grand_total: Mapped[str | None] = mapped_column(Numeric(18, 2), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Free-text — untrusted (FR-1.4); stored but never used as instructions.
    payment_terms: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        doc="Payment terms as stated on the invoice. Untrusted free-text (FR-1.4).",
    )

    # ---- Raw extraction payload (for audit / re-extraction) -----------------
    raw_extraction_payload: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="JSON string of the raw LLM output, stored for audit (FR-6.1).",
    )

    line_items: Mapped[list[InvoiceLineItemORM]] = relationship(
        "InvoiceLineItemORM",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class InvoiceLineItemBase(BaseModel):
    """A single invoice line item (FR-1.1)."""

    model_config = ConfigDict(from_attributes=True)

    line_number: int = Field(ge=1)
    # Free-text fields are untrusted input — validated for presence only (FR-1.4).
    description: str = Field(
        min_length=1,
        max_length=512,
        description="Item description (untrusted free-text, FR-1.4).",
    )
    qty: Decimal = Field(gt=0, description="Quantity billed.")
    unit_price: Decimal = Field(ge=0, description="Unit price billed.")
    amount: Decimal = Field(ge=0, description="Line total (qty × unit_price).")

    @field_validator("description")
    @classmethod
    def _desc_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be blank.")
        return v

    @field_validator("qty", "unit_price", "amount", mode="before")
    @classmethod
    def _to_decimal(cls, v: object) -> Decimal:
        try:
            return Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"Expected a numeric value, got {v!r}") from exc

    @model_validator(mode="after")
    def _amount_consistent(self) -> "InvoiceLineItemBase":
        """
        Amount must equal qty × unit_price within a small rounding tolerance.
        FR-1.3: we do NOT fabricate the amount — if it fails this check the
        caller must treat it as an extraction inconsistency.
        """
        computed = (self.qty * self.unit_price).quantize(Decimal("0.01"))
        stated = self.amount.quantize(Decimal("0.01"))
        tolerance = Decimal("0.02")  # allow ±2 cents for rounding
        if abs(computed - stated) > tolerance:
            raise ValueError(
                f"amount ({stated}) does not match qty × unit_price "
                f"({computed}). Possible extraction error."
            )
        return self


class InvoiceLineItemCreate(InvoiceLineItemBase):
    """Schema for creating a line item."""


class InvoiceLineItemRead(InvoiceLineItemBase):
    """Schema for reading a line item."""

    id: str
    invoice_id: str
    created_at: datetime
    updated_at: datetime


class InvoiceBase(BaseModel):
    """
    Fields shared by Invoice schema variants.

    All extracted fields are required for a fully-validated invoice (FR-1.1).
    The InvoiceCreate variant uses this for the extraction output path.
    """

    model_config = ConfigDict(from_attributes=True)

    invoice_number: str = Field(min_length=1, max_length=64)
    source_file_ref: str | None = Field(default=None, max_length=512)

    # Extracted required fields (FR-1.1) — all must be present for EXTRACTED status.
    vendor_name: str = Field(
        min_length=1,
        max_length=255,
        description="Extracted vendor name. Untrusted input (FR-1.4).",
    )
    invoice_date: date = Field(description="Invoice date (YYYY-MM-DD).")
    po_reference: str = Field(
        min_length=1,
        max_length=64,
        description="PO reference number on the invoice.",
    )
    contract_reference: str = Field(
        min_length=1,
        max_length=64,
        description="Contract reference on the invoice.",
    )
    subtotal: Decimal = Field(ge=0, description="Invoice subtotal.")
    tax: Decimal = Field(ge=0, description="Tax amount.")
    grand_total: Decimal = Field(gt=0, description="Grand total (must be > 0).")
    due_date: date = Field(description="Payment due date.")
    # Free-text — untrusted per FR-1.4.
    payment_terms: str = Field(
        min_length=1,
        max_length=128,
        description="Payment terms string. Untrusted free-text (FR-1.4).",
    )
    line_items: list[InvoiceLineItemBase] = Field(
        min_length=1,
        description="At least one line item is required.",
    )

    extraction_status: ExtractionStatus = Field(
        default=ExtractionStatus.PENDING,
    )
    invoice_status: InvoiceStatus = Field(
        default=InvoiceStatus.RECEIVED,
    )

    @field_validator("invoice_number", "vendor_name", "po_reference", "contract_reference")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field must not be blank or whitespace-only.")
        return v.strip()

    @field_validator("subtotal", "tax", "grand_total", mode="before")
    @classmethod
    def _to_decimal(cls, v: object) -> Decimal:
        try:
            return Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"Expected a numeric value, got {v!r}") from exc

    @model_validator(mode="after")
    def _due_date_after_invoice_date(self) -> "InvoiceBase":
        if self.due_date < self.invoice_date:
            raise ValueError(
                f"due_date ({self.due_date}) must not be before "
                f"invoice_date ({self.invoice_date})."
            )
        return self

    @model_validator(mode="after")
    def _grand_total_gte_subtotal(self) -> "InvoiceBase":
        if self.grand_total < self.subtotal:
            raise ValueError(
                "grand_total must be greater than or equal to subtotal."
            )
        return self


class InvoiceCreate(InvoiceBase):
    """Schema used when recording a newly extracted (validated) invoice."""

    line_items: list[InvoiceLineItemCreate] = Field(min_length=1)
    extraction_status: ExtractionStatus = Field(default=ExtractionStatus.EXTRACTED)
    invoice_status: InvoiceStatus = Field(default=InvoiceStatus.EXTRACTED)


class InvoiceRead(InvoiceBase):
    """Schema returned when reading an invoice."""

    id: str
    line_items: list[InvoiceLineItemRead]
    raw_extraction_payload: str | None = None
    created_at: datetime
    updated_at: datetime


class InvoiceReceived(BaseModel):
    """
    Minimal schema for an invoice that has been received but not yet extracted.

    Used when recording receipt before the extraction agent runs.
    """

    model_config = ConfigDict(from_attributes=True)

    invoice_number: str = Field(min_length=1, max_length=64)
    source_file_ref: str | None = Field(default=None)
    extraction_status: ExtractionStatus = Field(default=ExtractionStatus.PENDING)
    invoice_status: InvoiceStatus = Field(default=InvoiceStatus.RECEIVED)
