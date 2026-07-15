"""
models/base.py — SQLAlchemy declarative base and shared mixins.

All ORM table classes inherit from `Base`.  Tables that need a surrogate
primary key and audit timestamps inherit from `TimestampMixin` as well.

Design notes:
- UUID primary keys are stored as CHAR(36) strings in SQLite (which has no
  native UUID type); the `uuid` Python module produces them at Python level.
- `created_at` / `updated_at` are set by SQLAlchemy's `server_default` /
  `onupdate`, so they are always present even if the caller forgets to set them.
- The mixin is kept minimal — it only carries what every entity needs.
  Entity-specific fields live in each model file.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _new_uuid() -> str:
    """Return a new UUID4 string.  Used as the default for pk columns."""
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class TimestampMixin:
    """
    Adds `id`, `created_at`, and `updated_at` columns to any ORM table.

    `id`         — UUID4 string primary key, generated at Python level so the
                   value is available before the INSERT flushes to the DB.
    `created_at` — set once on INSERT via server_default; never updated.
    `updated_at` — set on INSERT and updated on every UPDATE via onupdate.
    """

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_uuid,
        doc="UUID4 surrogate primary key.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        doc="UTC timestamp of row creation.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        doc="UTC timestamp of last update.",
    )
