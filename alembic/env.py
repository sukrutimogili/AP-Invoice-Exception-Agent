"""
alembic/env.py — Alembic migration environment for LedgerGate-Agent.

Wired to:
  - models.Base.metadata  (autogenerate reads all ORM table definitions)
  - app.config.get_settings().database_url  (single source of truth for DB URL)

All ORM models are imported via `models` package so their tables are registered
on Base.metadata before autogenerate runs.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# ---------------------------------------------------------------------------
# Import all ORM models so their tables are registered on Base.metadata.
# This must happen before target_metadata is set.
# ---------------------------------------------------------------------------
import models  # noqa: F401 — side-effect import registers all ORM classes
from models.base import Base

# ---------------------------------------------------------------------------
# Load DATABASE_URL from pydantic-settings (spec.md §2 — single source of truth).
# ---------------------------------------------------------------------------
from app.config import get_settings

_settings = get_settings()

# Alembic config object
config = context.config

# Override sqlalchemy.url from our settings so alembic.ini never needs a
# hardcoded connection string.
config.set_main_option("sqlalchemy.url", _settings.database_url)

# Set up Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode (no live DB connection needed).

    Useful for generating SQL scripts.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Render column-level CHECK constraints from Pydantic-derived columns
        render_as_batch=True,   # required for SQLite ALTER TABLE support
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode (connects to the live DB).
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,   # required for SQLite ALTER TABLE support
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
