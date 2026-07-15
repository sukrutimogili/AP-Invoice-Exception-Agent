"""
models/contract.py — Contract + ContractLineItem + DiscountTerm domain models.

Requirements.md §6: 'Contract — contract reference, vendor, contracted unit
prices, discount terms, approval rules.'
FR-7.1: discount term fields (discount_pct, discount_days, net_days).
FR-2.6: contract not resolved → CONTRACT_NOT_FOUND exception.

Pydantic schemas  → DiscountTermSchema, ContractLineItemBase,
                    ContractBase, ContractCreate, ContractRead
SQLAlchemy tables → ContractLineItemORM, ContractORM
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# SQLAlchemy ORM tables
# ---------------------------------------------------------------------------


class ContractLineItemORM(Base, TimestampMixin):
    """Contracted unit price for a specific item / SKU."""

    __tablename__ = "contract_line_items"

    contract_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    unit_price: Mapped[str] = mapped_column(
        Numeric(18, 4),
        nullable=False,
        doc="Contracted unit price for this line.",
    )

    contract: Mapped["ContractORM"] = relationship(
        "ContractORM", back_populates="line_items"
    )


class ContractORM(Base, TimestampMixin):
    """Contract header — ties a vendor to negotiated prices and discount terms."""

    __tablename__ = "contracts"

    contract_reference: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        doc="Unique contract identifier referenced on invoices.",
    )
    vendor_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("vendors.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Discount term fields (FR-7.1) — nullable; a contract may have no discount.
    # Raw term string e.g. "2/10 net 30" for display / audit purposes.
    discount_term_raw: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc="Raw discount term string, e.g. '2/10 net 30' (FR-7.1).",
    )
    discount_pct: Mapped[str | None] = mapped_column(
        Numeric(7, 4),
        nullable=True,
        doc="Discount percentage (e.g. 0.02 for 2%). Null = no discount.",
    )
    discount_days: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        doc="Days within which payment qualifies for the discount.",
    )
    net_days: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        doc="Standard payment terms (days until due date).",
    )

    # Approval rules — denormalised threshold for contract-level approval context.
    approval_threshold: Mapped[str | None] = mapped_column(
        Numeric(18, 2),
        nullable=True,
        doc="Contract-level approval threshold, if different from global default.",
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    line_items: Mapped[list[ContractLineItemORM]] = relationship(
        "ContractLineItemORM",
        back_populates="contract",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class DiscountTermSchema(BaseModel):
    """
    Parsed discount term (FR-7.1).

    Example: "2/10 net 30" → discount_pct=0.02, discount_days=10, net_days=30.
    All three fields are required together; if any is absent the discount cannot
    be evaluated.
    """

    model_config = ConfigDict(from_attributes=True)

    discount_term_raw: str = Field(
        min_length=1,
        description="Original discount term string, e.g. '2/10 net 30'.",
    )
    discount_pct: Decimal = Field(
        gt=0,
        lt=1,
        description="Discount fraction (0 < x < 1). E.g. 0.02 for 2%.",
    )
    discount_days: int = Field(
        gt=0,
        description="Days within which payment qualifies for the discount.",
    )
    net_days: int = Field(
        gt=0,
        description="Standard net payment days.",
    )

    @model_validator(mode="after")
    def _discount_days_lt_net_days(self) -> "DiscountTermSchema":
        if self.discount_days >= self.net_days:
            raise ValueError(
                f"discount_days ({self.discount_days}) must be less than "
                f"net_days ({self.net_days})."
            )
        return self

    @field_validator("discount_pct", mode="before")
    @classmethod
    def _pct_to_decimal(cls, v: object) -> Decimal:
        try:
            d = Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"discount_pct must be numeric, got {v!r}") from exc
        if not (0 < d < 1):
            raise ValueError("discount_pct must be between 0 and 1 exclusive.")
        return d


class ContractLineItemBase(BaseModel):
    """A single contracted line item (price schedule)."""

    model_config = ConfigDict(from_attributes=True)

    line_number: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=512)
    unit_price: Decimal = Field(ge=0, description="Contracted unit price.")

    @field_validator("description")
    @classmethod
    def _desc_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be blank.")
        return v.strip()

    @field_validator("unit_price", mode="before")
    @classmethod
    def _price_to_decimal(cls, v: object) -> Decimal:
        try:
            d = Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"unit_price must be numeric, got {v!r}") from exc
        if d < 0:
            raise ValueError("unit_price must be non-negative.")
        return d


class ContractLineItemCreate(ContractLineItemBase):
    """Schema for creating a contract line item."""


class ContractLineItemRead(ContractLineItemBase):
    """Schema for reading a contract line item."""

    id: str
    contract_id: str
    created_at: datetime
    updated_at: datetime


class ContractBase(BaseModel):
    """Fields shared by Contract schema variants."""

    model_config = ConfigDict(from_attributes=True)

    contract_reference: str = Field(min_length=1, max_length=64)
    vendor_id: str = Field(min_length=1)
    discount_term: DiscountTermSchema | None = Field(
        default=None,
        description="Parsed discount term, if the contract offers one (FR-7.1).",
    )
    approval_threshold: Decimal | None = Field(
        default=None,
        ge=0,
        description="Contract-level approval override, if set.",
    )
    notes: str | None = Field(default=None)
    line_items: list[ContractLineItemBase] = Field(default_factory=list)

    @field_validator("contract_reference")
    @classmethod
    def _ref_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("contract_reference must not be blank.")
        return v.strip()

    @model_validator(mode="after")
    def _at_least_one_line(self) -> "ContractBase":
        if not self.line_items:
            raise ValueError("A Contract must have at least one line item.")
        return self


class ContractCreate(ContractBase):
    """Schema for creating a Contract."""

    line_items: list[ContractLineItemCreate] = Field(min_length=1)


class ContractRead(ContractBase):
    """Schema for reading a Contract."""

    id: str
    line_items: list[ContractLineItemRead]
    created_at: datetime
    updated_at: datetime
