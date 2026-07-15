"""
repositories/po_repo.py — query helpers for the purchase_orders table.

Phase 9 wiring point: the matching layer (matching/po_matcher.py or similar)
currently receives PO data from an in-process dict.  Replace that lookup with
get_po_by_number() once a database session is available.

Public API
----------
get_po_by_number(session, po_number) -> PurchaseOrderCreate | None
    Return the PO matching po_number (with all line items), mapped to a
    PurchaseOrderCreate Pydantic schema, or None if no row exists.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, selectinload

from models.purchase_order import (
    POLineItemCreate,
    PurchaseOrderCreate,
    PurchaseOrderORM,
)

__all__ = ["get_po_by_number"]


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
            line_items: list[POLineItemCreate] (line_number, description,
                        qty, unit_price) — sorted by line_number.

    Notes:
        - Line items are loaded in the same query via selectinload to avoid
          N+1 queries.  The list is sorted by line_number for determinism.
        - Returns None on no match; callers should treat this as FR-2.6
          PO_NOT_FOUND.
        - ORM Numeric columns are stored as Python Decimal-compatible strings
          by SQLAlchemy.  The Pydantic validators on POLineItemBase accept
          any numeric-coercible type, so no explicit conversion is needed.
    """
    row: PurchaseOrderORM | None = (
        session.query(PurchaseOrderORM)
        .options(selectinload(PurchaseOrderORM.line_items))
        .filter(PurchaseOrderORM.po_number == po_number)
        .first()
    )
    if row is None:
        return None

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
