"""
models/match_result.py — MatchResult + LineItemMatchDetail domain models.

Requirements.md §6: 'MatchResult — per-invoice field-by-field comparison output.'
FR-2.2: per-line item: qty variance, unit_price variance (absolute + percentage).
FR-2.3: grand_total variance vs PO total.
FR-2.4: approval threshold check.
FR-2.5: vendor_known check.
FR-2.6: po_resolved / contract_resolved checks.
FR-3.1: overall_passed = AND of all sub-checks.

Pydantic schemas  → LineItemMatchDetail, MatchResultBase, MatchResultCreate,
                    MatchResultRead
SQLAlchemy tables → MatchResultORM (with JSON-serialised line detail)
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Boolean, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# SQLAlchemy ORM table
# ---------------------------------------------------------------------------


class MatchResultORM(Base, TimestampMixin):
    """Stores the field-by-field match outcome for one invoice."""

    __tablename__ = "match_results"

    invoice_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        unique=True,          # one MatchResult per invoice
        nullable=False,
        index=True,
    )

    # Sub-check booleans (FR-3.1 conditions)
    vendor_known: Mapped[bool] = mapped_column(Boolean, nullable=False)
    po_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    contract_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    quantities_match: Mapped[bool] = mapped_column(Boolean, nullable=False)
    prices_match: Mapped[bool] = mapped_column(Boolean, nullable=False)
    total_matches: Mapped[bool] = mapped_column(Boolean, nullable=False)
    approval_satisfied: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Overall result
    overall_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Numeric deltas for traceability
    total_variance_abs: Mapped[str | None] = mapped_column(
        Numeric(18, 4), nullable=True, doc="Absolute variance: invoice total − PO total."
    )
    total_variance_pct: Mapped[str | None] = mapped_column(
        Numeric(10, 6), nullable=True, doc="Percentage variance relative to PO total."
    )

    # Per-line details serialised as JSON (avoids a heavy join for the common read path)
    line_item_details_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="JSON array of LineItemMatchDetail objects.",
    )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class LineItemMatchDetail(BaseModel):
    """Match detail for a single invoice line item (FR-2.2)."""

    model_config = ConfigDict(from_attributes=True)

    line_number: int = Field(ge=1)

    # Quantities
    billed_qty: Decimal = Field(description="Quantity on the invoice.")
    ordered_qty: Decimal = Field(description="Quantity on the PO.")
    qty_variance_abs: Decimal = Field(description="billed_qty − ordered_qty.")
    qty_match: bool = Field(description="True if variance is within tolerance.")

    # Unit prices
    billed_unit_price: Decimal = Field(description="Unit price on the invoice.")
    contract_unit_price: Decimal = Field(description="Contracted unit price.")
    price_variance_abs: Decimal = Field(description="billed_unit_price − contract_unit_price.")
    price_variance_pct: Decimal = Field(description="price_variance_abs / contract_unit_price.")
    price_match: bool = Field(description="True if variance is within tolerance.")

    @field_validator(
        "billed_qty", "ordered_qty", "qty_variance_abs",
        "billed_unit_price", "contract_unit_price",
        "price_variance_abs", "price_variance_pct",
        mode="before",
    )
    @classmethod
    def _to_decimal(cls, v: object) -> Decimal:
        try:
            return Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"Expected numeric, got {v!r}") from exc


class MatchResultBase(BaseModel):
    """Fields shared by MatchResult schema variants."""

    model_config = ConfigDict(from_attributes=True)

    invoice_id: str = Field(min_length=1)

    # FR-3.1 sub-checks
    vendor_known: bool
    po_resolved: bool
    contract_resolved: bool
    quantities_match: bool
    prices_match: bool
    total_matches: bool
    approval_satisfied: bool

    # Derived
    overall_passed: bool

    total_variance_abs: Decimal | None = Field(default=None)
    total_variance_pct: Decimal | None = Field(default=None)

    line_item_details: list[LineItemMatchDetail] = Field(default_factory=list)

    @field_validator("total_variance_abs", "total_variance_pct", mode="before")
    @classmethod
    def _opt_decimal(cls, v: object) -> Decimal | None:
        if v is None:
            return None
        try:
            return Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"Expected numeric or null, got {v!r}") from exc


class MatchResultCreate(MatchResultBase):
    """Schema for writing a new MatchResult."""


class MatchResultRead(MatchResultBase):
    """Schema for reading a MatchResult."""

    id: str
    created_at: datetime
    updated_at: datetime
