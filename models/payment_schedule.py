"""
models/payment_schedule.py — PaymentSchedule domain model.

Requirements.md §6: 'PaymentSchedule — invoice reference, scheduled date,
amount, discount taken (bool).'
FR-3.2: STP invoices are scheduled for payment automatically.
FR-7.3: discount_taken records whether the early-payment discount was taken.
FR-7.4: payment schedule is only created post-STP (never for exceptions).

Pydantic schemas  → PaymentScheduleBase, PaymentScheduleCreate,
                    PaymentScheduleRead
SQLAlchemy table  → PaymentScheduleORM
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# SQLAlchemy ORM table
# ---------------------------------------------------------------------------


class PaymentScheduleORM(Base, TimestampMixin):
    """Scheduled payment for an STP-approved invoice."""

    __tablename__ = "payment_schedules"

    invoice_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        unique=True,      # one schedule per invoice
        nullable=False,
        index=True,
    )
    scheduled_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        doc="Date the payment is scheduled for.",
    )
    amount: Mapped[str] = mapped_column(
        Numeric(18, 2),
        nullable=False,
        doc="Amount to be paid.",
    )
    discount_taken: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="True = early-payment discount applied (FR-7.3).",
    )
    discount_amount: Mapped[str | None] = mapped_column(
        Numeric(18, 2),
        nullable=True,
        doc="Discount amount saved if discount_taken is True.",
    )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class PaymentScheduleBase(BaseModel):
    """Fields shared by PaymentSchedule schema variants."""

    model_config = ConfigDict(from_attributes=True)

    invoice_id: str = Field(min_length=1)
    scheduled_date: date = Field(description="Scheduled payment date.")
    amount: Decimal = Field(gt=0, description="Payment amount. Must be positive.")
    discount_taken: bool = Field(
        default=False,
        description="True if early-payment discount is applied (FR-7.3).",
    )
    discount_amount: Decimal | None = Field(
        default=None,
        ge=0,
        description="Discount amount if discount_taken is True.",
    )

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_to_decimal(cls, v: object) -> Decimal:
        try:
            d = Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"amount must be numeric, got {v!r}") from exc
        if d <= 0:
            raise ValueError("amount must be positive.")
        return d

    @field_validator("discount_amount", mode="before")
    @classmethod
    def _discount_to_decimal(cls, v: object) -> Decimal | None:
        if v is None:
            return None
        try:
            d = Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"discount_amount must be numeric or null, got {v!r}") from exc
        if d < 0:
            raise ValueError("discount_amount must be non-negative.")
        return d

    @model_validator(mode="after")
    def _discount_fields_consistent(self) -> "PaymentScheduleBase":
        """
        If discount_taken is True, discount_amount must be provided and > 0.
        If discount_taken is False, discount_amount should be None or 0.
        """
        if self.discount_taken and (
            self.discount_amount is None or self.discount_amount <= 0
        ):
            raise ValueError(
                "discount_amount must be positive when discount_taken is True."
            )
        return self


class PaymentScheduleCreate(PaymentScheduleBase):
    """Schema for creating a new PaymentSchedule."""


class PaymentScheduleRead(PaymentScheduleBase):
    """Schema for reading a PaymentSchedule."""

    id: str
    created_at: datetime
    updated_at: datetime
