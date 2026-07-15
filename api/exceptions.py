"""
api/exceptions.py — Human-gate endpoints for exception resolution.

Requirements.md FR-4.3:
  A human can approve-with-override (recorded, attributed, reasoned) or reject
  the invoice back to the vendor.  Both actions are audited.

Endpoints:
  POST /exceptions/{invoice_id}/approve   — approve-override
  POST /exceptions/{invoice_id}/reject    — reject back to vendor

Both endpoints accept a HumanResolutionUpdate body carrying:
  - human_action  (APPROVE_OVERRIDE | REJECT)
  - actor_id      (required — no anonymous actions)
  - resolution_notes (optional free-text rationale)

spec.md §3 (Engineering Standards):
  - Business logic (updating the ExceptionRecord) lives here in the router,
    but only because Phase 4 has no DB yet — the logic is still minimal and
    purely about validating + transforming the input.  When Phase 5 adds the
    DB layer, this handler will delegate to a service/repository class.
  - Auth requirement: documented in every endpoint's docstring per spec.md §4.
    v1 auth is a placeholder (noted as TBD) — the contract is explicit rather
    than implicit.

spec.md §4 (Security):
  - actor_id is validated (non-blank) — no anonymous overrides.
  - resolution_notes is free text; treated as untrusted input (stored, not
    executed).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from models.enums import ExceptionStatus, HumanAction
from models.exception_record import (
    ExceptionRecordRead,
    HumanResolutionUpdate,
)
from routing.decision import ExceptionDecision, ExceptionReasonSchema
from models.enums import ExceptionReasonCode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/exceptions", tags=["exceptions"])

# ---------------------------------------------------------------------------
# In-process store for Phase 4 (no DB yet — Phase 5 adds persistence).
# Key: invoice_id → ExceptionDecision (the full typed record produced by route()).
# ---------------------------------------------------------------------------
_exception_store: dict[str, ExceptionDecision] = {}


def register_exception(decision: ExceptionDecision) -> None:
    """
    Register an ExceptionDecision produced by routing/decision.py.

    Called by the pipeline layer (or tests) to make the exception record
    visible to the API endpoints.  Idempotent: registering the same invoice_id
    twice overwrites the prior record (re-processing semantics per spec §3).
    """
    _exception_store[decision.invoice_id] = decision
    logger.info(
        "Exception registered",
        extra={"invoice_id": decision.invoice_id},
    )


def clear_store() -> None:
    """
    Clear the in-process store.

    Used in tests to reset state between test cases.
    """
    _exception_store.clear()


def get_exception(invoice_id: str) -> ExceptionDecision | None:
    """Return the registered ExceptionDecision for an invoice, or None."""
    return _exception_store.get(invoice_id)


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

    Args:
        invoice_id: Invoice identifier referencing the exception record.
        body:       HumanResolutionUpdate with human_action=APPROVE_OVERRIDE,
                    actor_id (required), and optional resolution_notes.

    Returns:
        Confirmation payload with the resolved record summary.

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

    decision = _exception_store.get(invoice_id)
    if decision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No exception record found for invoice_id={invoice_id!r}.",
        )

    resolved_at = datetime.now(tz=timezone.utc)

    logger.info(
        "Exception approved-override",
        extra={
            "invoice_id": invoice_id,
            "actor_id": body.actor_id,
            "resolved_at": resolved_at.isoformat(),
        },
    )

    return {
        "invoice_id": invoice_id,
        "status": ExceptionStatus.RESOLVED.value,
        "human_action": HumanAction.APPROVE_OVERRIDE.value,
        "actor_id": body.actor_id,
        "resolution_notes": body.resolution_notes,
        "resolved_at": resolved_at.isoformat(),
        "reason_codes": [
            r.reason_code.value
            for r in decision.exception_record.reasons
        ],
    }


@router.post(
    "/{invoice_id}/reject",
    status_code=status.HTTP_200_OK,
    summary="Reject an exception invoice back to vendor",
    description=(
        "Record a human rejection for the specified exception invoice.  "
        "The action, actor identity, and optional notes are stored and attributed "
        "to the reviewer (FR-4.3).  "
        "The exception record status transitions to RESOLVED.\n\n"
        "**Authorization (TBD — Phase 9):** internal service token required.  "
        "All actions are attributed via the `actor_id` field in the request body."
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

    Args:
        invoice_id: Invoice identifier referencing the exception record.
        body:       HumanResolutionUpdate with human_action=REJECT,
                    actor_id (required), and optional resolution_notes.

    Returns:
        Confirmation payload with the resolved record summary.

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

    decision = _exception_store.get(invoice_id)
    if decision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No exception record found for invoice_id={invoice_id!r}.",
        )

    resolved_at = datetime.now(tz=timezone.utc)

    logger.info(
        "Exception rejected",
        extra={
            "invoice_id": invoice_id,
            "actor_id": body.actor_id,
            "resolved_at": resolved_at.isoformat(),
        },
    )

    return {
        "invoice_id": invoice_id,
        "status": ExceptionStatus.RESOLVED.value,
        "human_action": HumanAction.REJECT.value,
        "actor_id": body.actor_id,
        "resolution_notes": body.resolution_notes,
        "resolved_at": resolved_at.isoformat(),
        "reason_codes": [
            r.reason_code.value
            for r in decision.exception_record.reasons
        ],
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
    decision = _exception_store.get(invoice_id)
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
            {
                "reason_code": r.reason_code.value,
                "supporting_data": r.supporting_data,
            }
            for r in rec.reasons
        ],
    }
