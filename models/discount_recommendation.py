"""
models/discount_recommendation.py — DiscountRecommendation domain model.

Requirements.md §6: 'DiscountRecommendation — invoice reference, discount %,
window, annualized return, hurdle rate, recommendation, taken/not-taken.'
FR-7.2: formula: (discount% / (1 − discount%)) × (365 / (net_days − discount_days))
FR-7.3: recommendation + inputs surfaced to AP clerk / controller.
FR-7.4: only runs on STP-eligible invoices.
FR-7.5: if window lapsed → WINDOW_MISSED, scheduled at standard terms.

Pydantic schemas  → DiscountRecommendationBase, DiscountRecommendationCreate,
                    DiscountRecommendationRead
SQLAlchemy table  → DiscountRecommendationORM
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Boolean, Date, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin
from models.enums import DiscountRecommendation


# ---------------------------------------------------------------------------
# SQLAlchemy ORM table
# ---------------------------------------------------------------------------


class DiscountRecommendationORM(Base, TimestampMixin):
    """Discount evaluation record for one STP-eligible invoice."""

    __tablename__ = "discount_recommendations"

    invoice_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # Inputs (FR-7.1 / FR-7.2)
    discount_pct: Mapped[str | None] = mapped_column(
        Numeric(7, 4), nullable=True, doc="Discount fraction (e.g. 0.02 for 2%)."
    )
    discount_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True, doc="Discount window in days."
    )
    net_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True, doc="Standard net terms in days."
    )
    invoice_amount: Mapped[str] = mapped_column(
        Numeric(18, 2), nullable=False, doc="Invoice grand total."
    )
    discount_amount: Mapped[str | None] = mapped_column(
        Numeric(18, 2), nullable=True, doc="Discount amount = invoice_amount × discount_pct."
    )

    # Computation result (FR-7.2)
    annualized_return: Mapped[str | None] = mapped_column(
        Numeric(10, 6),
        nullable=True,
        doc="Annualized return of taking the discount (FR-7.2 formula).",
    )
    hurdle_rate: Mapped[str] = mapped_column(
        Numeric(7, 4),
        nullable=False,
        doc="Cost-of-capital hurdle rate used for comparison.",
    )

    # Outcome
    recommendation: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="One of DiscountRecommendation enum values.",
    )
    discount_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        doc="Deadline for taking the discount (invoice_date + discount_days).",
    )
    window_missed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="True if the discount window had already lapsed (FR-7.5).",
    )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class DiscountRecommendationBase(BaseModel):
    """Fields shared by DiscountRecommendation schema variants."""

    model_config = ConfigDict(from_attributes=True)

    invoice_id: str = Field(min_length=1)
    invoice_amount: Decimal = Field(gt=0)

    # Discount inputs — nullable when no discount term exists on the contract.
    discount_pct: Decimal | None = Field(
        default=None,
        description="Discount fraction (0 < x < 1). Null if no discount term.",
    )
    discount_days: int | None = Field(default=None, gt=0)
    net_days: int | None = Field(default=None, gt=0)
    discount_amount: Decimal | None = Field(default=None, ge=0)

    annualized_return: Decimal | None = Field(
        default=None,
        description="Annualized effective return (FR-7.2). Null if window missed or no discount.",
    )
    hurdle_rate: Decimal = Field(
        gt=0,
        lt=1,
        description="Configured hurdle rate (cost of capital).",
    )
    recommendation: DiscountRecommendation
    discount_date: date | None = Field(default=None)
    window_missed: bool = Field(default=False)

    @field_validator("invoice_amount", "hurdle_rate", mode="before")
    @classmethod
    def _required_decimal(cls, v: object) -> Decimal:
        try:
            return Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"Expected numeric, got {v!r}") from exc

    @field_validator("discount_pct", "discount_amount", "annualized_return", mode="before")
    @classmethod
    def _opt_decimal(cls, v: object) -> Decimal | None:
        if v is None:
            return None
        try:
            return Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"Expected numeric or null, got {v!r}") from exc

    @model_validator(mode="after")
    def _discount_term_complete(self) -> "DiscountRecommendationBase":
        """
        If any discount term field is set, all three must be set (FR-7.1).
        """
        term_fields = (self.discount_pct, self.discount_days, self.net_days)
        if any(f is not None for f in term_fields) and not all(
            f is not None for f in term_fields
        ):
            raise ValueError(
                "discount_pct, discount_days, and net_days must all be set together "
                "or all be None (FR-7.1)."
            )
        return self

    @model_validator(mode="after")
    def _window_missed_consistent(self) -> "DiscountRecommendationBase":
        if self.window_missed and self.recommendation != DiscountRecommendation.WINDOW_MISSED:
            raise ValueError(
                "recommendation must be WINDOW_MISSED when window_missed is True (FR-7.5)."
            )
        return self


class DiscountRecommendationCreate(DiscountRecommendationBase):
    """Schema for creating a DiscountRecommendation."""


class DiscountRecommendationRead(DiscountRecommendationBase):
    """Schema for reading a DiscountRecommendation."""

    id: str
    created_at: datetime
    updated_at: datetime
