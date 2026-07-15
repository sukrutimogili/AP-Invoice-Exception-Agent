"""
services/payment_store.py — In-process payment schedule store.

Holds the PaymentScheduleCreate records produced by routing/decision.py.
Contains no FastAPI, no HTTP concerns.

Both the service layer (services/invoice_service.py) and the API endpoints
(api/payments.py) import from here.  The UI can also query the store
directly without touching the API layer.

When Phase 9 wires a database session, replace the dict body in each function
with ORM calls — all callers remain unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from models.payment_schedule import PaymentScheduleCreate

logger = logging.getLogger(__name__)

# In-process store.  Key: invoice_id → PaymentScheduleCreate.
_store: dict[str, PaymentScheduleCreate] = {}


def register_payment_schedule(schedule: PaymentScheduleCreate) -> None:
    """
    Persist a PaymentScheduleCreate produced by route().

    Idempotent: re-registering the same invoice_id overwrites the prior record.
    """
    _store[schedule.invoice_id] = schedule
    logger.info(
        "Payment schedule registered",
        extra={
            "invoice_id": schedule.invoice_id,
            "scheduled_date": str(schedule.scheduled_date),
            "amount": str(schedule.amount),
            "discount_taken": schedule.discount_taken,
        },
    )


def get_payment_schedule(invoice_id: str) -> PaymentScheduleCreate | None:
    """Return the PaymentScheduleCreate for an invoice, or None."""
    return _store.get(invoice_id)


def list_payment_schedules() -> list[PaymentScheduleCreate]:
    """Return all registered payment schedules."""
    return list(_store.values())


def clear_store() -> None:
    """Clear all records.  Only for use in tests."""
    _store.clear()


def serialise(schedule: PaymentScheduleCreate) -> dict[str, Any]:
    """Convert a PaymentScheduleCreate to a JSON-safe dict."""
    return {
        "invoice_id": schedule.invoice_id,
        "scheduled_date": str(schedule.scheduled_date),
        "amount": str(schedule.amount),
        "discount_taken": schedule.discount_taken,
        "discount_amount": str(schedule.discount_amount)
        if schedule.discount_amount is not None
        else None,
    }
