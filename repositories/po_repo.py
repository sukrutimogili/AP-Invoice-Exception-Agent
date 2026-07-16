"""
repositories/po_repo.py — query helpers for the purchase_orders table.

Public API
----------
get_po_by_number(session, po_number) -> PurchaseOrderCreate | None
    Return the PO matching po_number (with all line items), mapped to a
    PurchaseOrderCreate Pydantic schema, or None if no row exists.

upsert_po(session, po) -> UpsertResult[PurchaseOrderCreate]
    Insert a new PO row if no row exists for po.po_number, or compare the
    incoming PO against the existing one and return:
      - UpsertCreated   if a new row was written.
      - UpsertUnchanged if an identical row already exists (no write).
      - UpsertConflict  if the row exists but fields differ (no write).

    Fields compared on conflict detection
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    Header:   po_total, approval_threshold, notes
    Lines:    per-line unit_price and description for each line_number that
              exists in both documents; extra/missing lines also count as
              conflicts (reported as line_items[N].unit_price).

    The vendor_id FK is NOT compared — it is expected to change as the
    raw extracted vendor_code is resolved to a real UUID between uploads.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Union

from sqlalchemy.orm import Session, selectinload

from models.purchase_order import (
    POLineItemCreate,
    POLineItemORM,
    PurchaseOrderCreate,
    PurchaseOrderORM,
)
from repositories.upsert_result import (
    FieldDiff,
    UpsertConflict,
    UpsertCreated,
    UpsertResult,
    UpsertUnchanged,
)

__all__ = ["get_po_by_number", "upsert_po"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _orm_to_create(row: PurchaseOrderORM) -> PurchaseOrderCreate:
    """Map a PurchaseOrderORM row (with loaded line_items) to PurchaseOrderCreate."""
    line_items = [
        POLineItemCreate(
            line_number=li.line_number,
            description=li.description,
            qty=li.qty,
            unit_price=li.unit_price,
        )
        for li in sorted(row.line_items, key=lambda li: li.line_number)
    ]
    return PurchaseOrderCreate(
        po_number=row.po_number,
        vendor_id=row.vendor_id,
        po_total=row.po_total,
        approval_threshold=row.approval_threshold,
        notes=row.notes,
        line_items=line_items,
    )


def _decimal_eq(a: object, b: object) -> bool:
    """Compare two values that may be Decimal, str, int, or float for numeric equality."""
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except Exception:
        return str(a) == str(b)


def _diff_po(
    existing: PurchaseOrderCreate,
    incoming: PurchaseOrderCreate,
) -> dict[str, FieldDiff]:
    """
    Return a field-level diff between *existing* (stored) and *incoming* (upload).

    Only fields that disagree are included.  vendor_id is intentionally
    excluded (the FK is resolved externally and is expected to change).

    Header fields compared:   po_total, approval_threshold, notes
    Line fields compared:     per line_number — unit_price, description, qty
    Structural line changes:  lines present in one but not the other are
                              reported as "<key> missing in existing/incoming".
    """
    diff: dict[str, FieldDiff] = {}

    # --- Header fields ---
    if not _decimal_eq(existing.po_total, incoming.po_total):
        diff["po_total"] = FieldDiff(existing=existing.po_total, incoming=incoming.po_total)

    if not _decimal_eq(existing.approval_threshold, incoming.approval_threshold):
        diff["approval_threshold"] = FieldDiff(
            existing=existing.approval_threshold,
            incoming=incoming.approval_threshold,
        )

    if (existing.notes or "") != (incoming.notes or ""):
        diff["notes"] = FieldDiff(existing=existing.notes, incoming=incoming.notes)

    # --- Line items ---
    existing_by_line = {li.line_number: li for li in existing.line_items}
    incoming_by_line = {li.line_number: li for li in incoming.line_items}

    all_line_numbers = sorted(
        set(existing_by_line) | set(incoming_by_line)
    )

    for n in all_line_numbers:
        ex_li = existing_by_line.get(n)
        in_li = incoming_by_line.get(n)

        if ex_li is None:
            # Line exists in incoming but not in existing.
            diff[f"line_items[{n}].unit_price"] = FieldDiff(
                existing=None,
                incoming=in_li.unit_price,  # type: ignore[union-attr]
            )
            continue

        if in_li is None:
            # Line exists in existing but not in incoming.
            diff[f"line_items[{n}].unit_price"] = FieldDiff(
                existing=ex_li.unit_price,
                incoming=None,
            )
            continue

        # Both sides have this line — compare each field.
        if not _decimal_eq(ex_li.unit_price, in_li.unit_price):
            diff[f"line_items[{n}].unit_price"] = FieldDiff(
                existing=ex_li.unit_price,
                incoming=in_li.unit_price,
            )

        if ex_li.description.strip() != in_li.description.strip():
            diff[f"line_items[{n}].description"] = FieldDiff(
                existing=ex_li.description,
                incoming=in_li.description,
            )

        if not _decimal_eq(ex_li.qty, in_li.qty):
            diff[f"line_items[{n}].qty"] = FieldDiff(
                existing=ex_li.qty,
                incoming=in_li.qty,
            )

    return diff


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_po_by_number(session: Session, po_number: str) -> PurchaseOrderCreate | None:
    """
    Look up a Purchase Order by its unique po_number, eagerly loading line items.

    Args:
        session:   Active SQLAlchemy Session (from db.session.get_session).
        po_number: The PO number to search for (FR-2.1), e.g. "PO-2024-001".

    Returns:
        PurchaseOrderCreate populated from the ORM row and its line items,
        or None if no matching PO exists.

        Mapped fields:
            po_number, vendor_id, po_total, approval_threshold, notes
            line_items: list[POLineItemCreate] sorted by line_number.

    Notes:
        - Line items are loaded via selectinload to avoid N+1 queries.
        - Returns None on no match; callers treat this as FR-2.6 PO_NOT_FOUND.
    """
    row: PurchaseOrderORM | None = (
        session.query(PurchaseOrderORM)
        .options(selectinload(PurchaseOrderORM.line_items))
        .filter(PurchaseOrderORM.po_number == po_number)
        .first()
    )
    if row is None:
        return None
    return _orm_to_create(row)


def upsert_po(
    session: Session,
    po: PurchaseOrderCreate,
) -> UpsertResult:
    """
    Insert or compare a Purchase Order identified by po.po_number.

    Behaviour
    ---------
    1. If no row exists for po.po_number:
       - INSERT the row (header + line items) inside the caller's transaction.
       - Return UpsertCreated with the freshly mapped record.

    2. If a row exists and ALL compared fields are identical:
       - Return UpsertUnchanged.  No write is performed.

    3. If a row exists and one or more compared fields differ:
       - Return UpsertConflict with the *existing* record and a field-level
         diff (dict[str, FieldDiff]).  No write is performed — the caller
         decides whether to overwrite, raise, or flag for review.

    Compared fields
    ---------------
    Header:  po_total, approval_threshold, notes
    Lines:   unit_price, description, qty per line_number; structural line
             differences (extra / missing lines) are also conflicts.

    vendor_id is NOT compared (the FK may legitimately differ between an
    extraction-time placeholder and a DB-resolved UUID).

    Args:
        session: Active SQLAlchemy Session.  The caller is responsible for
                 calling session.commit() / session.rollback().
        po:      Fully validated PurchaseOrderCreate to insert or compare.

    Returns:
        UpsertCreated | UpsertUnchanged | UpsertConflict
    """
    existing_row: PurchaseOrderORM | None = (
        session.query(PurchaseOrderORM)
        .options(selectinload(PurchaseOrderORM.line_items))
        .filter(PurchaseOrderORM.po_number == po.po_number)
        .first()
    )

    # ------------------------------------------------------------------ #
    # Case 1: no existing row — INSERT                                    #
    # ------------------------------------------------------------------ #
    if existing_row is None:
        new_row = PurchaseOrderORM(
            po_number=po.po_number,
            vendor_id=po.vendor_id,
            po_total=str(po.po_total),
            approval_threshold=str(po.approval_threshold),
            notes=po.notes,
            line_items=[
                POLineItemORM(
                    line_number=li.line_number,
                    description=li.description,
                    qty=str(li.qty),
                    unit_price=str(li.unit_price),
                )
                for li in po.line_items
            ],
        )
        session.add(new_row)
        session.flush()  # assign PK without committing; caller owns the transaction
        session.refresh(new_row)
        # Reload with line_items so _orm_to_create works.
        session.refresh(new_row)
        # Re-query to get eager-loaded line_items after flush.
        created_row: PurchaseOrderORM = (
            session.query(PurchaseOrderORM)
            .options(selectinload(PurchaseOrderORM.line_items))
            .filter(PurchaseOrderORM.po_number == po.po_number)
            .one()
        )
        return UpsertCreated(record=_orm_to_create(created_row))

    # ------------------------------------------------------------------ #
    # Case 2 / 3: existing row — compare and decide                       #
    # ------------------------------------------------------------------ #
    existing_schema = _orm_to_create(existing_row)
    diff = _diff_po(existing_schema, po)

    if not diff:
        return UpsertUnchanged(record=existing_schema)

    return UpsertConflict(record=existing_schema, diff=diff)
