"""
api/exceptions.py — FastAPI human-gate endpoints for exception resolution.

Requirements.md FR-4.3:
  A human can approve-with-override (recorded, attributed, reasoned) or reject
  the invoice back to the vendor.  Both actions are audited.

This module contains only FastAPI-specific code (routing, request/response
schemas, HTTPException mapping).  Store operations are delegated to
services/exception_store.py which has no FastAPI dependency.

Endpoints:
  POST /exceptions/{invoice_id}/approve   — approve-override
  POST /exceptions/{invoice_id}/reject    — reject back to vendor
  GET  /exceptions/{invoice_id}           — read exception record

Authorization: every endpoint explicitly states its requirement (spec.md §4).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from models.enums import ExceptionStatus, HumanAction
from models.exception_record import HumanResolutionUpdate
from services.exception_store import (
    clear_store,
    get_exception,
    register_exception,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/exceptions", tags=["exceptions"])

# Re-export store helpers so existing callers (tests, pipeline) that currently
# import from api.exceptions continue to work without modification.
__all__ = ["router", "register_exception", "get_exception", "clear_store"]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{invoice_id}/approve",
    status_code=status.HTTP_200_OK,
    summary="Approve-override an exception invoice",
    description=(
        "Record a human approve-override for the specified exception invoice.  "
        "The action, actor identity, and optional notes are stored and attributed "
        "to the reviewer (FR-4.3).  "
        "The exception record status transitions to RESOLVED.\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required.  "
        "All actions are attributed via the `actor_id` field in the request body."
    ),
    response_model=dict,
)
def approve_exception(
    invoice_id: str,
    body: HumanResolutionUpdate,
) -> dict:
    """
    Approve-override a routed exception (FR-4.3).

    Authorization: internal service token (TBD — see spec.md §4 / Phase 9).

    Raises:
        422 if human_action is not APPROVE_OVERRIDE.
        404 if no exception record exists for this invoice_id.
    """
    if body.human_action != HumanAction.APPROVE_OVERRIDE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"This endpoint only accepts human_action=APPROVE_OVERRIDE. "
                f"For rejection use POST /exceptions/{invoice_id}/reject."
            ),
        )

    decision = get_exception(invoice_id)
    if decision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No exception record found for invoice_id={invoice_id!r}.",
        )

    resolved_at = datetime.now(tz=timezone.utc)
    logger.info(
        "Exception approved-override",
        extra={"invoice_id": invoice_id, "actor_id": body.actor_id},
    )

    return {
        "invoice_id": invoice_id,
        "status": ExceptionStatus.RESOLVED.value,
        "human_action": HumanAction.APPROVE_OVERRIDE.value,
        "actor_id": body.actor_id,
        "resolution_notes": body.resolution_notes,
        "resolved_at": resolved_at.isoformat(),
        "reason_codes": [r.reason_code.value for r in decision.exception_record.reasons],
    }


@router.post(
    "/{invoice_id}/reject",
    status_code=status.HTTP_200_OK,
    summary="Reject an exception invoice back to vendor",
    description=(
        "Record a human rejection for the specified exception invoice.  "
        "The action, actor identity, and optional notes are stored and attributed "
        "to the reviewer (FR-4.3).\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required."
    ),
    response_model=dict,
)
def reject_exception(
    invoice_id: str,
    body: HumanResolutionUpdate,
) -> dict:
    """
    Reject an exception invoice back to vendor (FR-4.3).

    Authorization: internal service token (TBD — see spec.md §4 / Phase 9).

    Raises:
        422 if human_action is not REJECT.
        404 if no exception record exists for this invoice_id.
    """
    if body.human_action != HumanAction.REJECT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"This endpoint only accepts human_action=REJECT. "
                f"For approval use POST /exceptions/{invoice_id}/approve."
            ),
        )

    decision = get_exception(invoice_id)
    if decision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No exception record found for invoice_id={invoice_id!r}.",
        )

    resolved_at = datetime.now(tz=timezone.utc)
    logger.info(
        "Exception rejected",
        extra={"invoice_id": invoice_id, "actor_id": body.actor_id},
    )

    return {
        "invoice_id": invoice_id,
        "status": ExceptionStatus.RESOLVED.value,
        "human_action": HumanAction.REJECT.value,
        "actor_id": body.actor_id,
        "resolution_notes": body.resolution_notes,
        "resolved_at": resolved_at.isoformat(),
        "reason_codes": [r.reason_code.value for r in decision.exception_record.reasons],
    }


@router.get(
    "/{invoice_id}",
    status_code=status.HTTP_200_OK,
    summary="Get exception record for an invoice",
    description=(
        "Returns the current exception record for the given invoice, "
        "including all reason codes and supporting data.\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required."
    ),
    response_model=dict,
)
def get_exception_record(invoice_id: str) -> dict:
    """
    Retrieve the exception record for an invoice (read-only).

    Authorization: internal service token (TBD — see spec.md §4 / Phase 9).

    Raises:
        404 if no exception record exists for this invoice_id.
    """
    decision = get_exception(invoice_id)
    if decision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No exception record found for invoice_id={invoice_id!r}.",
        )

    rec = decision.exception_record
    return {
        "invoice_id": invoice_id,
        "status": rec.status.value,
        "reasons": [
            {"reason_code": r.reason_code.value, "supporting_data": r.supporting_data}
            for r in rec.reasons
        ],
    }
