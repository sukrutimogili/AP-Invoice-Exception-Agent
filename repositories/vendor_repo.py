"""
repositories/vendor_repo.py — query and upsert helpers for the vendor master table.

Public API
----------
get_vendor_by_id(session, vendor_id) -> VendorCreate | None
get_vendor_by_code(session, vendor_code) -> VendorCreate | None
upsert_vendor(session, vendor) -> UpsertResult[VendorCreate]
    Insert a new vendor if none exists for vendor.vendor_code, or compare the
    incoming record against the existing one:
      - UpsertCreated   — no prior row; inserted and returned.
      - UpsertUnchanged — existing row matches on all compared fields; no write.
      - UpsertConflict  — existing row differs on one or more fields; no write.
                          diff lists which fields disagreed and their values.

    Fields compared (natural-key vendor_code excluded):
        name, is_active, contact_email, notes
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from models.vendor import VendorCreate, VendorORM
from repositories.upsert_result import (
    FieldDiff,
    UpsertConflict,
    UpsertCreated,
    UpsertResult,
    UpsertUnchanged,
)

__all__ = ["get_vendor_by_code", "get_vendor_by_id", "list_vendors", "upsert_vendor"]


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def get_vendor_by_id(session: Session, vendor_id: str) -> VendorCreate | None:
    """
    Look up a vendor by its surrogate primary key (UUID string).

    Used when the vendor_id is already known — typically resolved from a PO row
    (PurchaseOrderORM.vendor_id) so we avoid fragile name-based matching.
    """
    row: VendorORM | None = session.get(VendorORM, vendor_id)
    if row is None:
        return None
    return VendorCreate.model_validate(row)


def get_vendor_by_code(session: Session, vendor_code: str) -> VendorCreate | None:
    """
    Look up a vendor by its unique vendor_code.

    Returns VendorCreate or None.  Does not raise on a miss — callers are
    responsible for treating None as FR-2.5 VENDOR_NOT_APPROVED.
    """
    row: VendorORM | None = (
        session.query(VendorORM)
        .filter(VendorORM.vendor_code == vendor_code)
        .first()
    )
    if row is None:
        return None
    return VendorCreate.model_validate(row)


def list_vendors(session: Session) -> list[VendorCreate]:
    """
    Return all vendor rows from the vendor master table.

    Used by agents and dashboards that need to iterate over every vendor —
    e.g. the vendor risk agent (agents/vendor_risk_agent.py).

    Returns an empty list if no vendors exist yet.  Never raises on an empty
    table.

    Args:
        session: Active SQLAlchemy Session.

    Returns:
        List of VendorCreate objects, one per row, in no guaranteed order.
    """
    rows: list[VendorORM] = session.query(VendorORM).all()
    return [VendorCreate.model_validate(row) for row in rows]


# ---------------------------------------------------------------------------
# Internal diff helper
# ---------------------------------------------------------------------------


def _diff_vendor(
    existing: VendorCreate,
    incoming: VendorCreate,
) -> dict[str, FieldDiff]:
    """
    Return a field-level diff for the compared vendor fields.

    vendor_code is the natural key and is not compared (it was the lookup
    key, so it is always equal by definition).

    Fields compared: name, is_active, contact_email, notes.
    Only differing fields are included in the returned dict.
    """
    diff: dict[str, FieldDiff] = {}

    if existing.name != incoming.name:
        diff["name"] = FieldDiff(existing=existing.name, incoming=incoming.name)

    if existing.is_active != incoming.is_active:
        diff["is_active"] = FieldDiff(
            existing=existing.is_active, incoming=incoming.is_active
        )

    # Treat None and empty-string as equivalent for nullable text fields
    # so that a round-trip through the DB (which stores NULL) compares equal
    # to a VendorCreate with contact_email=None.
    if (existing.contact_email or "") != (incoming.contact_email or ""):
        diff["contact_email"] = FieldDiff(
            existing=existing.contact_email, incoming=incoming.contact_email
        )

    if (existing.notes or "") != (incoming.notes or ""):
        diff["notes"] = FieldDiff(existing=existing.notes, incoming=incoming.notes)

    return diff


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def upsert_vendor(
    session: Session,
    vendor: VendorCreate,
) -> UpsertResult:
    """
    Insert or compare a Vendor identified by vendor.vendor_code.

    Behaviour
    ---------
    1. No row for vendor.vendor_code → INSERT → return UpsertCreated.
    2. Existing row, all compared fields identical → UpsertUnchanged (no write).
    3. Existing row, one or more compared fields differ → UpsertConflict with
       the stored record and a field-level diff.  No write is performed; the
       caller decides how to proceed.

    Compared fields: name, is_active, contact_email, notes.
    vendor_code is the natural key used for lookup and is never in the diff.

    Args:
        session: Active SQLAlchemy Session.  Caller owns commit/rollback.
        vendor:  Fully validated VendorCreate to insert or compare.

    Returns:
        UpsertCreated | UpsertUnchanged | UpsertConflict
    """
    existing_row: VendorORM | None = (
        session.query(VendorORM)
        .filter(VendorORM.vendor_code == vendor.vendor_code)
        .first()
    )

    # ------------------------------------------------------------------ #
    # Case 1: no existing row — INSERT                                    #
    # ------------------------------------------------------------------ #
    if existing_row is None:
        new_row = VendorORM(
            vendor_code=vendor.vendor_code,
            name=vendor.name,
            contact_email=vendor.contact_email,
            is_active=vendor.is_active,
            notes=vendor.notes,
        )
        session.add(new_row)
        session.flush()  # caller owns the transaction; assign PK without committing
        session.refresh(new_row)
        return UpsertCreated(record=VendorCreate.model_validate(new_row))

    # ------------------------------------------------------------------ #
    # Case 2 / 3: existing row — compare                                  #
    # ------------------------------------------------------------------ #
    existing_schema = VendorCreate.model_validate(existing_row)
    diff = _diff_vendor(existing_schema, vendor)

    if not diff:
        return UpsertUnchanged(record=existing_schema)

    return UpsertConflict(record=existing_schema, diff=diff)
