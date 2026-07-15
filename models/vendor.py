"""
models/vendor.py — Vendor domain model.

Requirements.md §6: 'Vendor — vendor master record, active/approved flag.'
FR-2.5: vendor must exist in the approved vendor master; unknown vendors are
        always an exception regardless of match quality.

Pydantic schema  → VendorBase / VendorCreate / VendorRead
SQLAlchemy table → VendorORM
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# SQLAlchemy ORM table
# ---------------------------------------------------------------------------


class VendorORM(Base, TimestampMixin):
    """Approved vendor master record."""

    __tablename__ = "vendors"

    # Natural business key — must be unique across the vendor master.
    vendor_code: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        doc="Unique vendor identifier (e.g. ERP vendor code).",
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="Legal / trading name of the vendor.",
    )

    contact_email: Mapped[str | None] = mapped_column(
        String(254),
        nullable=True,
        doc="Primary contact e-mail for AP correspondence.",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        doc="True = vendor is in the approved vendor master (FR-2.5).",
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Free-text internal notes. Treated as untrusted data (spec.md §4).",
    )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class VendorBase(BaseModel):
    """Fields shared by all Vendor schema variants."""

    model_config = ConfigDict(from_attributes=True)

    vendor_code: str = Field(
        min_length=1,
        max_length=64,
        description="Unique vendor identifier.",
    )
    name: str = Field(
        min_length=1,
        max_length=255,
        description="Legal / trading name.",
    )
    contact_email: str | None = Field(
        default=None,
        max_length=254,
        description="Primary AP contact e-mail.",
    )
    is_active: bool = Field(
        default=True,
        description="True = vendor is approved (FR-2.5).",
    )
    notes: str | None = Field(
        default=None,
        description="Internal notes (untrusted free-text).",
    )

    @field_validator("vendor_code")
    @classmethod
    def _vendor_code_no_whitespace(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("vendor_code must not be blank or whitespace-only.")
        return stripped

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be blank or whitespace-only.")
        return stripped

    @field_validator("contact_email")
    @classmethod
    def _email_basic_format(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError(f"contact_email '{v}' does not look like a valid e-mail address.")
        return v


class VendorCreate(VendorBase):
    """Schema used when creating a new vendor record."""


class VendorRead(VendorBase):
    """Schema returned when reading a vendor record (includes DB-generated fields)."""

    id: str
    created_at: datetime
    updated_at: datetime
