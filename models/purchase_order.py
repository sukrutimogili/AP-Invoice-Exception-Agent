"""
models/purchase_order.py — PurchaseOrder + POLineItem domain models.

Requirements.md §6: 'PurchaseOrder — PO number, vendor, line items, PO total,
approval threshold context.'
FR-2.1: system retrieves the referenced PO by PO number.
FR-2.2: line-item-level qty / unit_price comparison against invoice.
FR-2.6: if PO cannot be resolved → PO_NOT_FOUND exception (not silent).

Pydantic schemas  → POLineItemBase, PurchaseOrderBase, PurchaseOrderCreate,
                    PurchaseOrderRead
SQLAlchemy tables → POLineItemORM, PurchaseOrderORM
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# SQLAlchemy ORM tables
# ---------------------------------------------------------------------------


class POLineItemORM(Base, TimestampMixin):
    """A single line on a Purchase Order."""

    __tablename__ = "po_line_items"

    purchase_order_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="1-based position of this line on the PO.",
    )
    description: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )
    qty: Mapped[str] = mapped_column(
        Numeric(18, 4),
        nullable=False,
        doc="Ordered quantity (stored as NUMERIC for precision).",
    )
    unit_price: Mapped[str] = mapped_column(
        Numeric(18, 4),
        nullable=False,
        doc="Contracted unit price.",
    )

    purchase_order: Mapped["PurchaseOrderORM"] = relationship(
        "PurchaseOrderORM", back_populates="line_items"
    )


class PurchaseOrderORM(Base, TimestampMixin):
    """Purchase Order header."""

    __tablename__ = "purchase_orders"

    po_number: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        doc="Unique PO number — the key used to resolve a PO from an invoice (FR-2.1).",
    )
    vendor_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("vendors.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    po_total: Mapped[str] = mapped_column(
        Numeric(18, 2),
        nullable=False,
        doc="Total value of the PO (used for grand-total comparison, FR-2.3).",
    )
    # approval_threshold_context stores the threshold that was in effect when
    # the PO was raised (controller-configurable, FR-2.4).  Stored denormalised
    # so historical records are stable even if the global setting changes.
    approval_threshold: Mapped[str] = mapped_column(
        Numeric(18, 2),
        nullable=False,
        doc="Approval threshold in effect for this PO (FR-2.4).",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    line_items: Mapped[list[POLineItemORM]] = relationship(
        "POLineItemORM",
        back_populates="purchase_order",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class POLineItemBase(BaseModel):
    """A single PO line item."""

    model_config = ConfigDict(from_attributes=True)

    line_number: int = Field(
        ge=1,
        description="1-based line number.",
    )
    description: str = Field(
        min_length=1,
        max_length=512,
    )
    qty: Decimal = Field(
        gt=0,
        description="Ordered quantity. Must be positive.",
    )
    unit_price: Decimal = Field(
        ge=0,
        description="Contracted unit price. Must be non-negative.",
    )

    @field_validator("description")
    @classmethod
    def _desc_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be blank.")
        return v.strip()

    @field_validator("qty", mode="before")
    @classmethod
    def _qty_to_decimal(cls, v: object) -> Decimal:
        try:
            d = Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"qty must be a numeric value, got {v!r}") from exc
        if d <= 0:
            raise ValueError("qty must be greater than 0.")
        return d

    @field_validator("unit_price", mode="before")
    @classmethod
    def _price_to_decimal(cls, v: object) -> Decimal:
        try:
            d = Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"unit_price must be a numeric value, got {v!r}") from exc
        if d < 0:
            raise ValueError("unit_price must be non-negative.")
        return d


class POLineItemCreate(POLineItemBase):
    """Schema for creating a PO line item."""


class POLineItemRead(POLineItemBase):
    """Schema for reading a PO line item (includes DB-generated fields)."""

    id: str
    purchase_order_id: str
    created_at: datetime
    updated_at: datetime


class PurchaseOrderBase(BaseModel):
    """Fields shared by PurchaseOrder schema variants."""

    model_config = ConfigDict(from_attributes=True)

    po_number: str = Field(
        min_length=1,
        max_length=64,
        description="Unique PO number (FR-2.1).",
    )
    vendor_id: str = Field(
        min_length=1,
        description="FK to vendors.id.",
    )
    po_total: Decimal = Field(
        ge=0,
        description="Total PO value (FR-2.3).",
    )
    approval_threshold: Decimal = Field(
        gt=0,
        description="Approval threshold in effect for this PO (FR-2.4).",
    )
    notes: str | None = Field(default=None)
    line_items: list[POLineItemBase] = Field(
        default_factory=list,
        description="Must have at least one line item.",
    )

    @field_validator("po_number")
    @classmethod
    def _po_number_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("po_number must not be blank.")
        return v.strip()

    @field_validator("po_total", mode="before")
    @classmethod
    def _total_to_decimal(cls, v: object) -> Decimal:
        try:
            return Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"po_total must be numeric, got {v!r}") from exc

    @field_validator("approval_threshold", mode="before")
    @classmethod
    def _threshold_to_decimal(cls, v: object) -> Decimal:
        try:
            d = Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"approval_threshold must be numeric, got {v!r}") from exc
        if d <= 0:
            raise ValueError("approval_threshold must be positive.")
        return d

    @model_validator(mode="after")
    def _at_least_one_line(self) -> "PurchaseOrderBase":
        if not self.line_items:
            raise ValueError("A PurchaseOrder must have at least one line item.")
        return self


class PurchaseOrderCreate(PurchaseOrderBase):
    """Schema for creating a PurchaseOrder."""

    line_items: list[POLineItemCreate] = Field(min_length=1)


class PurchaseOrderRead(PurchaseOrderBase):
    """Schema for reading a PurchaseOrder."""

    id: str
    line_items: list[POLineItemRead]
    created_at: datetime
    updated_at: datetime
