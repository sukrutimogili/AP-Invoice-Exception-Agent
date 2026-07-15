"""
models/audit_event.py — AuditEvent domain model.

Requirements.md §6: 'AuditEvent — append-only log entry linked to an invoice,
covering every state transition.'
FR-6.1: raw extraction, validation, matching, routing, reason codes, timestamps,
        human action and identity.
FR-6.2: queryable by invoice number, vendor, PO, date range, outcome.
FR-6.3: append-only; corrections create a new record referencing the original —
        no update or delete path is exposed at the application layer.
spec.md §4: audit logs are append-only at the application layer.

Enforcement of append-only:
  - AuditEventORM has NO updated_at column (from TimestampMixin it is excluded).
  - AuditEventCreate is the only write schema; there is no Update schema.
  - The ORM class does not include updated_at to make the intent explicit.

Pydantic schemas  → AuditEventCreate, AuditEventRead
SQLAlchemy table  → AuditEventORM
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, _new_uuid
from models.enums import AuditEventType


# ---------------------------------------------------------------------------
# SQLAlchemy ORM table (intentionally NO updated_at — append-only)
# ---------------------------------------------------------------------------


class AuditEventORM(Base):
    """
    Immutable audit log entry.

    Deliberately does NOT inherit TimestampMixin because we must not expose
    an `updated_at` column — that would imply rows can be modified.
    `id` and `created_at` are the only timestamp columns (FR-6.3).
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_uuid,
        doc="UUID4 primary key.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        doc="UTC timestamp of event creation.  Never updated.",
    )

    # ---- Correlation keys ---------------------------------------------------
    invoice_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Invoice this event belongs to (FR-6.2 query key).",
    )
    invoice_number: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        doc="Denormalised invoice number for direct query without join (FR-6.2).",
    )
    vendor_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        doc="Denormalised vendor name for direct query (FR-6.2).",
    )
    po_reference: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        doc="Denormalised PO reference for direct query (FR-6.2).",
    )

    # ---- Event payload ------------------------------------------------------
    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        doc="One of AuditEventType values.",
    )
    # JSON-serialised payload — shape varies by event_type.
    # Contains: raw extraction output, validation result, match fields,
    # routing decision, reason codes, human action + identity (FR-6.1).
    payload_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="JSON payload for this event (FR-6.1).",
    )

    # Actor — nullable for system-generated events; required for human actions.
    actor_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        doc="Identity of the human actor, if applicable (FR-4.3, FR-6.1).",
    )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class AuditEventCreate(BaseModel):
    """
    Schema for writing a new audit event.

    This is the ONLY write schema.  There is deliberately no AuditEventUpdate.
    To correct an error, create a new AuditEvent that references the original
    (FR-6.3).
    """

    model_config = ConfigDict(from_attributes=True)

    invoice_id: str = Field(min_length=1)
    event_type: AuditEventType
    payload_json: str | None = Field(
        default=None,
        description="JSON-serialised event payload (FR-6.1).",
    )
    actor_id: str | None = Field(
        default=None,
        max_length=128,
        description="Human actor identity, if applicable.",
    )

    # Denormalised correlation fields (populated from the invoice at write time)
    invoice_number: str | None = Field(default=None, max_length=64)
    vendor_name: str | None = Field(default=None, max_length=255)
    po_reference: str | None = Field(default=None, max_length=64)

    @field_validator("actor_id")
    @classmethod
    def _actor_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("actor_id must not be blank if provided.")
        return v


class AuditEventRead(BaseModel):
    """Schema for reading an audit event.  No write fields — read-only."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    invoice_id: str
    invoice_number: str | None
    vendor_name: str | None
    po_reference: str | None
    event_type: AuditEventType
    payload_json: str | None
    actor_id: str | None
