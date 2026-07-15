"""
repositories/contract_repo.py — query helpers for the contracts table.

Phase 9 wiring point: the discount/matching layer currently receives contract
data from an in-process dict.  Replace that lookup with get_contract_by_reference()
once a database session is available.

Public API
----------
get_contract_by_reference(session, contract_reference) -> ContractCreate | None
    Return the contract matching contract_reference (with all line items),
    mapped to a ContractCreate Pydantic schema, or None if no row exists.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, selectinload

from models.contract import (
    ContractCreate,
    ContractLineItemCreate,
    ContractORM,
    DiscountTermSchema,
)

__all__ = ["get_contract_by_reference"]


def _build_discount_term(row: ContractORM) -> DiscountTermSchema | None:
    """
    Assemble a DiscountTermSchema from the four flat ORM columns, or return
    None if the contract carries no discount term.

    A discount term is present only when all four columns are non-null:
    discount_term_raw, discount_pct, discount_days, net_days.  A partial
    record (e.g. discount_pct set but net_days NULL) is treated as no term
    rather than raising — the data quality problem should be caught at write
    time, not silently surfaced as a validation error on every read.
    """
    if (
        row.discount_term_raw is None
        or row.discount_pct is None
        or row.discount_days is None
        or row.net_days is None
    ):
        return None

    return DiscountTermSchema(
        discount_term_raw=row.discount_term_raw,
        discount_pct=row.discount_pct,
        discount_days=row.discount_days,
        net_days=row.net_days,
    )


def get_contract_by_reference(
    session: Session, contract_reference: str
) -> ContractCreate | None:
    """
    Look up a Contract by its unique contract_reference, eagerly loading line items.

    Args:
        session:            Active SQLAlchemy Session (from db.session.get_session).
        contract_reference: The contract reference string to search for
                            (FR-2.6), e.g. "CTR-2024-ACME".

    Returns:
        ContractCreate populated from the ORM row and its line items, or None
        if no matching contract exists.

        Mapped fields:
            contract_reference, vendor_id, discount_term (DiscountTermSchema
            or None), approval_threshold, notes
            line_items: list[ContractLineItemCreate] (line_number, description,
                        unit_price) — sorted by line_number.

    Notes:
        - Line items are loaded in the same query via selectinload to avoid
          N+1 queries.  The list is sorted by line_number for determinism.
        - The four flat discount columns (discount_term_raw, discount_pct,
          discount_days, net_days) are assembled into a DiscountTermSchema
          via _build_discount_term(); None is returned when any column is NULL.
        - Returns None on no match; callers should treat this as FR-2.6
          CONTRACT_NOT_FOUND.
    """
    row: ContractORM | None = (
        session.query(ContractORM)
        .options(selectinload(ContractORM.line_items))
        .filter(ContractORM.contract_reference == contract_reference)
        .first()
    )
    if row is None:
        return None

    line_items = [
        ContractLineItemCreate(
            line_number=li.line_number,
            description=li.description,
            unit_price=li.unit_price,
        )
        for li in sorted(row.line_items, key=lambda li: li.line_number)
    ]

    return ContractCreate(
        contract_reference=row.contract_reference,
        vendor_id=row.vendor_id,
        discount_term=_build_discount_term(row),
        approval_threshold=row.approval_threshold,
        notes=row.notes,
        line_items=line_items,
    )
