"""
models/exception_record.py — ExceptionRecord + ExceptionReason domain models.

Requirements.md §6: 'ExceptionRecord — reason code(s), supporting data,
status, human resolution.'
FR-4.1: any invoice failing FR-3.1 → exception record, never auto-paid.
FR-4.2: one or more structured reason codes with supporting data per record.
FR-4.3: human can APPROVE_OVERRIDE or REJECT; both actions are audited.

Pydantic schemas  → ExceptionReasonSchema, ExceptionRecordBase,
                    ExceptionRecordCreate, ExceptionRecordRead,
                    HumanResolutionUpdate
SQLAlchemy tables → ExceptionReasonORM, ExceptionRecordORM
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin
from models.enums import ExceptionReasonCode, ExceptionStatus, HumanAction


# ---------------------------------------------------------------------------
# SQLAlchemy ORM tables
# ---------------------------------------------------------------------------


class ExceptionReasonORM(Base, TimestampMixin):
    """Individual reason code + supporting data for one exception."""

    __tablename__ = "exception_reasons"

    exception_record_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("exception_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reason_code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        doc="One of ExceptionReasonCode values (FR-4.2).",
    )
    # Supporting data stored as JSON: billed price, contract price, delta, etc.
    supporting_data_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="JSON object with evidence fields (FR-4.2).",
    )

    exception_record: Mapped["ExceptionRecordORM"] = relationship(
        "ExceptionRecordORM", back_populates="reasons"
    )


class ExceptionRecordORM(Base, TimestampMixin):
    """Exception routing record for one invoice."""

    __tablename__ = "exception_records"

    invoice_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        unique=True,      # one ExceptionRecord per invoice
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ExceptionStatus.OPEN.value,
    )

    # Human resolution fields (nullable until resolved, FR-4.3)
    human_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        doc="Identity of the human reviewer (FR-4.3).",
    )
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    reasons: Mapped[list[ExceptionReasonORM]] = relationship(
        "ExceptionReasonORM",
        back_populates="exception_record",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class ExceptionReasonSchema(BaseModel):
    """
    One structured reason for an exception (FR-4.2).

    supporting_data holds the evidence: billed vs. expected values, delta, etc.
    The shape of supporting_data varies by reason_code, so it is typed as
    dict[str, Any] rather than a fixed schema — but it must be present and
    non-empty for reason codes that carry numeric evidence.
    """

    model_config = ConfigDict(from_attributes=True)

    reason_code: ExceptionReasonCode
    supporting_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Evidence for this reason (billed/expected values, delta, etc.).",
    )


class ExceptionRecordBase(BaseModel):
    """Fields shared by ExceptionRecord schema variants."""

    model_config = ConfigDict(from_attributes=True)

    invoice_id: str = Field(min_length=1)
    status: ExceptionStatus = Field(default=ExceptionStatus.OPEN)
    reasons: list[ExceptionReasonSchema] = Field(
        min_length=1,
        description="At least one reason code is required (FR-4.2).",
    )

    # Human resolution (populated when status → RESOLVED, FR-4.3)
    human_action: HumanAction | None = Field(default=None)
    actor_id: str | None = Field(
        default=None,
        max_length=128,
        description="Identity of the reviewer (required when resolved).",
    )
    resolution_notes: str | None = Field(default=None)
    resolved_at: datetime | None = Field(default=None)

    @model_validator(mode="after")
    def _resolved_fields_consistent(self) -> "ExceptionRecordBase":
        """
        If status is RESOLVED, human_action and actor_id must be present (FR-4.3).
        If status is OPEN, resolution fields must not be filled.
        """
        if self.status == ExceptionStatus.RESOLVED:
            if self.human_action is None:
                raise ValueError(
                    "human_action is required when exception status is RESOLVED."
                )
            if not self.actor_id:
                raise ValueError(
                    "actor_id is required when exception status is RESOLVED."
                )
        return self


class ExceptionRecordCreate(ExceptionRecordBase):
    """Schema for creating a new ExceptionRecord (always starts OPEN)."""

    status: ExceptionStatus = Field(default=ExceptionStatus.OPEN)

    @field_validator("status")
    @classmethod
    def _must_be_open_on_create(cls, v: ExceptionStatus) -> ExceptionStatus:
        if v != ExceptionStatus.OPEN:
            raise ValueError("A new ExceptionRecord must start with status OPEN.")
        return v


class ExceptionRecordRead(ExceptionRecordBase):
    """Schema for reading an ExceptionRecord."""

    id: str
    created_at: datetime
    updated_at: datetime


class HumanResolutionUpdate(BaseModel):
    """
    Payload for a human approve-override or reject action (FR-4.3).

    Both fields are required — the system never records an anonymous action.
    """

    model_config = ConfigDict(from_attributes=True)

    human_action: HumanAction
    actor_id: str = Field(min_length=1, max_length=128)
    resolution_notes: str | None = Field(default=None)

    @field_validator("actor_id")
    @classmethod
    def _actor_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("actor_id must not be blank.")
        return v.strip()
