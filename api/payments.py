"""
api/payments.py — Payment schedule query endpoints.

Requirements.md FR-3.2:
  STP invoices are scheduled for payment automatically.  This module exposes
  the read path so callers (AP clerks, controllers, tests) can query the
  current payment schedule without touching the DB layer directly.

FR-7.3:
  The discount_taken flag and discount_amount are surfaced here so the AP
  clerk / controller can see whether the early-payment discount was applied.

Endpoints:
  GET /payments/{invoice_id}   — retrieve the payment schedule for one invoice
  GET /payments/               — list all scheduled payments (paginated)

Store:
  Phase 9 uses an in-process dict store (same pattern as api/exceptions.py).
  The store is keyed by invoice_id.  A real deployment would back this with
  the PaymentScheduleORM table; swapping the store requires changing only the
  three helper functions at the bottom of this file.

Authorization:
  Every endpoint documents its authorization requirement explicitly per
  spec.md §4.  v1 auth is a single internal-service token (TBD — the contract
  is stated, not left implicit).

Rate limiting:
  Query endpoints are read-only and low-volume; no per-endpoint rate limit is
  applied.  The ingestion endpoint (api/invoices.py) carries the rate limit
  note for write operations (spec.md §4).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from models.payment_schedule import PaymentScheduleCreate, PaymentScheduleRead

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])

# ---------------------------------------------------------------------------
# In-process store for Phase 9 (no DB session yet — mirrors api/exceptions.py).
# Key: invoice_id → PaymentScheduleCreate
# ---------------------------------------------------------------------------
_payment_store: dict[str, PaymentScheduleCreate] = {}


# ---------------------------------------------------------------------------
# Store management helpers — replace bodies here to swap to a DB-backed store
# ---------------------------------------------------------------------------


def register_payment_schedule(schedule: PaymentScheduleCreate) -> None:
    """
    Register a PaymentSchedule produced by routing/decision.py → route().

    Called by the pipeline (api/invoices.py) after a STPDecision.
    Idempotent: re-registering the same invoice_id overwrites the prior record
    (re-processing semantics per spec.md §3).

    Args:
        schedule: The PaymentScheduleCreate returned by STPDecision.payment_schedule.
    """
    _payment_store[schedule.invoice_id] = schedule
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
    """Return the registered PaymentScheduleCreate for an invoice, or None."""
    return _payment_store.get(invoice_id)


def list_payment_schedules() -> list[PaymentScheduleCreate]:
    """Return all registered payment schedules, ordered by invoice_id (stable)."""
    return list(_payment_store.values())


def clear_store() -> None:
    """
    Clear the in-process payment schedule store.

    Only for use in tests to reset state between test cases.
    Never call from application code.
    """
    _payment_store.clear()


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _serialise(schedule: PaymentScheduleCreate) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/",
    status_code=status.HTTP_200_OK,
    summary="List all payment schedules",
    description=(
        "Returns all payment schedules currently registered in the system, "
        "in invoice_id order.  Each entry corresponds to one STP-approved "
        "invoice (FR-3.2).  Exception and rejected invoices are never present "
        "here (FR-7.4 / FR-4.1).\n\n"
        "Supports optional `limit` and `offset` query parameters for "
        "pagination.\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required.  "
        "Pass the token in the `Authorization: Bearer <token>` header.  "
        "Unauthenticated requests will be rejected with HTTP 401 once auth "
        "middleware is wired in Phase 9 deployment."
    ),
    response_model=dict,
)
def list_schedules(
    offset: int = Query(default=0, ge=0, description="Number of records to skip."),
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Maximum records to return (1–1000).",
    ),
) -> dict:
    """
    List all payment schedules with pagination (FR-3.2).

    Authorization: internal service token (TBD — see spec.md §4 / Phase 9).

    Args:
        offset: Zero-based pagination offset.
        limit:  Maximum number of results (1–1000).

    Returns:
        Paginated list of payment schedule summaries.
    """
    all_schedules = list_payment_schedules()
    page = all_schedules[offset : offset + limit]

    logger.debug(
        "Payment schedules listed",
        extra={"total": len(all_schedules), "offset": offset, "limit": limit},
    )

    return {
        "total": len(all_schedules),
        "offset": offset,
        "limit": limit,
        "schedules": [_serialise(s) for s in page],
    }


@router.get(
    "/{invoice_id}",
    status_code=status.HTTP_200_OK,
    summary="Get payment schedule for one invoice",
    description=(
        "Returns the payment schedule for the specified invoice.  "
        "Only STP-approved invoices have a payment schedule (FR-3.2).  "
        "Exception and rejected invoices return HTTP 404 here — see "
        "`GET /exceptions/{invoice_id}` for their status.\n\n"
        "The `discount_taken` flag indicates whether the early-payment "
        "discount was applied (FR-7.3), and `discount_amount` shows the "
        "dollar saving if applicable.\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required.  "
        "Pass the token in the `Authorization: Bearer <token>` header."
    ),
    response_model=dict,
)
def get_schedule(invoice_id: str) -> dict:
    """
    Get the payment schedule for one STP-approved invoice (FR-3.2, FR-7.3).

    Authorization: internal service token (TBD — see spec.md §4 / Phase 9).

    Args:
        invoice_id: The invoice identifier.

    Returns:
        Payment schedule with scheduled_date, amount, and discount fields.

    Raises:
        404 if no payment schedule exists for this invoice_id.  This occurs
            for exception invoices, rejected invoices, or unknown IDs.
    """
    schedule = get_payment_schedule(invoice_id)
    if schedule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No payment schedule found for invoice_id={invoice_id!r}.  "
                "This invoice may be an exception (check GET /exceptions/{invoice_id}) "
                "or may not have been submitted yet."
            ),
        )

    logger.debug("Payment schedule retrieved", extra={"invoice_id": invoice_id})
    return _serialise(schedule)
