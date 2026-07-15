"""
api/audit.py — Read-only audit trail query endpoints.

requirements.md FR-6.2:
  Audit records are queryable by invoice number, vendor, PO, date range,
  and outcome.

Endpoints:
  GET /audit/invoice/{invoice_id}         — all events for one invoice
  GET /audit/search                       — filter by vendor, PO, date range,
                                            event_type (outcome proxy)

All endpoints are strictly read-only.  No write, update, or delete endpoint
exists here — enforcing the append-only requirement at the API layer.

Authorization: TBD Phase 9 — internal service token required.  Documented
explicitly per spec.md §4 rather than left implicit.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from audit.writer import get_all_events
from models.enums import AuditEventType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["audit"])


# ---------------------------------------------------------------------------
# Internal query helper
# ---------------------------------------------------------------------------


def _events_for_invoice(invoice_id: str) -> list[dict[str, Any]]:
    """Return all audit events for a given invoice_id, in insertion order."""
    return [e for e in get_all_events() if e["invoice_id"] == invoice_id]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/invoice/{invoice_id}",
    status_code=status.HTTP_200_OK,
    summary="Get full audit trail for one invoice",
    description=(
        "Returns every audit event recorded for the specified invoice, "
        "in the order they were written.  The sequence reconstructs the full "
        "decision history without re-running the agent (FR-6.1).\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required."
    ),
    response_model=dict,
)
def get_invoice_audit_trail(invoice_id: str) -> dict:
    """
    Full ordered audit trail for one invoice (FR-6.1, FR-6.2).

    Authorization: internal service token (TBD — Phase 9).

    Raises:
        404 if no audit events exist for this invoice_id.
    """
    events = _events_for_invoice(invoice_id)
    if not events:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No audit events found for invoice_id={invoice_id!r}.",
        )

    logger.debug("Audit trail queried", extra={"invoice_id": invoice_id, "count": len(events)})
    return {
        "invoice_id": invoice_id,
        "event_count": len(events),
        "events": [_serialise_event(e) for e in events],
    }


@router.get(
    "/search",
    status_code=status.HTTP_200_OK,
    summary="Search audit events",
    description=(
        "Filter audit events by one or more criteria.  All filters are optional "
        "and combined with AND logic.  Returns events in insertion order.\n\n"
        "Queryable by (FR-6.2): invoice_number, vendor_name, po_reference, "
        "event_type (outcome), date range (from_dt / to_dt).\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required."
    ),
    response_model=dict,
)
def search_audit_events(
    invoice_number: str | None = Query(default=None, description="Filter by invoice number"),
    vendor_name: str | None = Query(default=None, description="Filter by vendor name (case-insensitive substring)"),
    po_reference: str | None = Query(default=None, description="Filter by PO reference"),
    event_type: str | None = Query(default=None, description="Filter by event type (e.g. STP_APPROVED)"),
    from_dt: datetime | None = Query(default=None, description="Filter events at or after this UTC datetime"),
    to_dt: datetime | None = Query(default=None, description="Filter events at or before this UTC datetime"),
) -> dict:
    """
    Search audit events with optional filters (FR-6.2).

    Authorization: internal service token (TBD — Phase 9).

    All parameters are optional.  With no parameters, returns all events.
    """
    # Validate event_type if provided.
    if event_type is not None:
        valid = {e.value for e in AuditEventType}
        if event_type not in valid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid event_type {event_type!r}. Valid values: {sorted(valid)}",
            )

    events = get_all_events()

    # Apply filters.
    if invoice_number is not None:
        events = [e for e in events if e.get("invoice_number") == invoice_number]
    if vendor_name is not None:
        vn_lower = vendor_name.lower()
        events = [
            e for e in events
            if e.get("vendor_name") and vn_lower in e["vendor_name"].lower()
        ]
    if po_reference is not None:
        events = [e for e in events if e.get("po_reference") == po_reference]
    if event_type is not None:
        events = [e for e in events if e.get("event_type") == event_type]
    if from_dt is not None:
        # created_at is stored as a datetime object in the dict after model_dump().
        events = [
            e for e in events
            if _parse_dt(e.get("created_at")) >= from_dt.replace(tzinfo=from_dt.tzinfo)
        ]
    if to_dt is not None:
        events = [
            e for e in events
            if _parse_dt(e.get("created_at")) <= to_dt.replace(tzinfo=to_dt.tzinfo)
        ]

    return {
        "count": len(events),
        "filters": {
            "invoice_number": invoice_number,
            "vendor_name": vendor_name,
            "po_reference": po_reference,
            "event_type": event_type,
            "from_dt": from_dt.isoformat() if from_dt else None,
            "to_dt": to_dt.isoformat() if to_dt else None,
        },
        "events": [_serialise_event(e) for e in events],
    }


@router.get(
    "/invoice/{invoice_id}/outcome",
    status_code=status.HTTP_200_OK,
    summary="Get the final outcome of an invoice",
    description=(
        "Returns the most recent terminal event for the invoice "
        "(STP_APPROVED, EXCEPTION_RAISED, HUMAN_OVERRIDE_APPROVED, "
        "HUMAN_REJECTED, or EXTRACTION_FAILED).  "
        "Useful for querying outcome without loading the full trail.\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required."
    ),
    response_model=dict,
)
def get_invoice_outcome(invoice_id: str) -> dict:
    """
    Return the final outcome event for an invoice (FR-6.2 — queryable by outcome).

    Authorization: internal service token (TBD — Phase 9).

    Raises:
        404 if no audit events exist for this invoice_id.
    """
    events = _events_for_invoice(invoice_id)
    if not events:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No audit events found for invoice_id={invoice_id!r}.",
        )

    # Terminal event types in priority order (latest wins for human actions).
    terminal = {
        AuditEventType.HUMAN_OVERRIDE_APPROVED.value,
        AuditEventType.HUMAN_REJECTED.value,
        AuditEventType.STP_APPROVED.value,
        AuditEventType.EXCEPTION_RAISED.value,
        AuditEventType.EXTRACTION_FAILED.value,
    }
    terminal_events = [e for e in events if e.get("event_type") in terminal]
    # Latest terminal event is the effective outcome.
    outcome_event = terminal_events[-1] if terminal_events else events[-1]

    return {
        "invoice_id": invoice_id,
        "outcome": outcome_event.get("event_type"),
        "event": _serialise_event(outcome_event),
    }


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _serialise_event(e: dict[str, Any]) -> dict[str, Any]:
    """Convert a stored event dict to a JSON-safe representation."""
    return {
        "id": e.get("id"),
        "created_at": str(e.get("created_at")),
        "invoice_id": e.get("invoice_id"),
        "invoice_number": e.get("invoice_number"),
        "vendor_name": e.get("vendor_name"),
        "po_reference": e.get("po_reference"),
        "event_type": e.get("event_type"),
        "actor_id": e.get("actor_id"),
        "payload_json": e.get("payload_json"),
    }


def _parse_dt(value: Any) -> datetime:
    """Parse a stored created_at value back to a datetime (it may already be one)."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
