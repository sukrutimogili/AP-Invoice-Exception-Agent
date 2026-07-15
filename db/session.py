"""
db/session.py — SQLAlchemy engine, session factory, and session dependency.

The engine is built once from settings.database_url (app/config.py).  All
application code that needs a database session must use get_session() — either
as a context manager in service-layer code, or as a FastAPI dependency via
Depends(get_session).

When Phase 9 wires up the real database session in services/exception_store.py
and services/payment_store.py, replace their in-process dict bodies by
injecting a session from get_session() and querying the ORM tables instead.

Design decisions
----------------
- engine is module-level so the connection pool is created once per process,
  not once per request.
- SessionLocal is a plain sessionmaker (not async) — sufficient for the
  current synchronous FastAPI routes.  Upgrading to AsyncSession later only
  requires swapping sessionmaker for async_sessionmaker here and updating
  route signatures.
- get_session() is a context-manager generator so it can be used both as
  `with get_session() as session:` in non-FastAPI code and as
  `Depends(get_session)` in FastAPI route signatures without duplication.
- Base is imported from models.base — this module never creates a second
  declarative base.  All ORM table metadata lives on Base.metadata.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from models.base import Base  # shared declarative base — never create a second one

__all__ = ["engine", "SessionLocal", "get_session", "Base"]

# ---------------------------------------------------------------------------
# Engine — built once from settings.database_url.
#
# connect_args={"check_same_thread": False} is required for SQLite because
# SQLAlchemy's connection pool may hand the same connection to a different
# thread than the one that created it.  The flag is harmless on other
# databases (Postgres, MySQL) because they have proper thread-safe drivers.
# ---------------------------------------------------------------------------
_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    # SQLite-specific: allow the connection to be used across threads.
    # Ignored by non-SQLite dialects.
    connect_args={"check_same_thread": False}
    if _settings.database_url.startswith("sqlite")
    else {},
    # Echo SQL to the logger at DEBUG level only — avoids noise in production
    # while still being useful when LOG_LEVEL=DEBUG.
    echo=_settings.log_level == "DEBUG",
)

# ---------------------------------------------------------------------------
# Session factory — autocommit=False, autoflush=False are the SQLAlchemy
# recommended defaults for web applications using explicit transactions.
# ---------------------------------------------------------------------------
SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=True,
)


# ---------------------------------------------------------------------------
# get_session() — context-manager / FastAPI dependency.
#
# Usage in service-layer code:
#     with get_session() as session:
#         session.add(obj)
#         session.commit()
#
# Usage as a FastAPI dependency:
#     def my_route(session: Session = Depends(get_session)):
#         ...
#
# The session is always closed in the finally block, returning its connection
# to the pool.  Callers are responsible for calling session.commit() before
# the context exits; on any exception the transaction is rolled back.
# ---------------------------------------------------------------------------
@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Yield a SQLAlchemy Session, rolling back on exception and always closing.

    Yields:
        Session: a bound, transaction-scoped SQLAlchemy session.

    Raises:
        Re-raises any exception after rolling back the transaction so the
        caller can handle or log it as appropriate.
    """
    session: Session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
