"""
services/exception_store.py — In-process exception record store.

Holds the ExceptionDecision records produced by routing/decision.py.
Contains no FastAPI, no HTTP concerns.

Both the service layer (services/invoice_service.py) and the API endpoints
(api/exceptions.py) import from here.  The UI can also query the store
directly without touching the API layer.

When Phase 9 wires a database session, replace the dict body in each function
with ORM calls — all callers remain unchanged.
"""

from __future__ import annotations

import logging

from routing.decision import ExceptionDecision

logger = logging.getLogger(__name__)

# In-process store.  Key: invoice_id → ExceptionDecision.
_store: dict[str, ExceptionDecision] = {}


def register_exception(decision: ExceptionDecision) -> None:
    """
    Persist an ExceptionDecision produced by route().

    Idempotent: re-registering the same invoice_id overwrites the prior record
    (re-processing semantics per spec.md §3).
    """
    _store[decision.invoice_id] = decision
    logger.info("Exception registered", extra={"invoice_id": decision.invoice_id})


def get_exception(invoice_id: str) -> ExceptionDecision | None:
    """Return the ExceptionDecision for an invoice, or None."""
    return _store.get(invoice_id)


def list_exceptions() -> list[ExceptionDecision]:
    """Return all registered ExceptionDecisions."""
    return list(_store.values())


def clear_store() -> None:
    """Clear all records.  Only for use in tests."""
    _store.clear()
