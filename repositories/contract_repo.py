"""
repositories/contract_repo.py — query helpers for the contracts table.

Public API
----------
get_contract_by_reference(session, contract_reference) -> ContractCreate | None
    Return the contract matching contract_reference (with all line items),
    mapped to a ContractCreate Pydantic schema, or None if no row exists.

upsert_contract(session, contract, *, discount_term_raw) -> UpsertResult[ContractCreate]
    Insert a new contract row if no row exists for contract.contract_reference,
    or compare the incoming contract against the existing one and return:
      - UpsertCreated   if a new row was written.
      - UpsertUnchanged if an identical row already exists (no write).
      - UpsertConflict  if the row exists but fields differ (no write).

    Fields compared on conflict detection
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    Header:   discount_term_raw, approval_threshold, notes
    Lines:    per-line unit_price and description for each line_number;
              extra/missing lines also count as conflicts.

    vendor_id is NOT compared — it is expected to differ between an
    extraction-time placeholder and a DB-resolved UUID.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session, selectinload

from models.contract import (
    ContractCreate,
    ContractLineItemCreate,
    ContractLineItemORM,
    ContractORM,
    DiscountTermSchema,
)
from repositories.upsert_result import (
    FieldDiff,
    UpsertConflict,
    UpsertCreated,
    UpsertResult,
    UpsertUnchanged,
)

__all__ = ["get_contract_by_reference", "upsert_contract"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_discount_term(row: ContractORM) -> DiscountTermSchema | None:
    """
    Assemble a DiscountTermSchema from the four flat ORM columns, or return
    None if any column is NULL.
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


def _orm_to_create(row: ContractORM) -> ContractCreate:
    """Map a ContractORM row (with loaded line_items) to ContractCreate."""
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


def _decimal_eq(a: object, b: object) -> bool:
    """Compare two values that may be Decimal, str, int, or float for numeric equality."""
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except Exception:
        return str(a) == str(b)


def _diff_contract(
    existing: ContractCreate,
    incoming: ContractCreate,
    incoming_discount_term_raw: str | None,
) -> dict[str, FieldDiff]:
    """
    Return a field-level diff between *existing* (stored) and *incoming* (upload).

    Only disagreeing fields are included.  vendor_id is excluded.

    Args:
        existing:                   ContractCreate mapped from the stored ORM row.
        incoming:                   ContractCreate being uploaded.
        incoming_discount_term_raw: Raw discount term string from the upload.
                                    Compared directly against the stored
                                    discount_term_raw column because the parsed
                                    DiscountTermSchema on ContractCreate may have
                                    been constructed independently of the raw string.
    """
    diff: dict[str, FieldDiff] = {}

    # --- discount_term_raw ---
    existing_raw = (
        existing.discount_term.discount_term_raw
        if existing.discount_term is not None
        else None
    )
    if (existing_raw or "") != (incoming_discount_term_raw or ""):
        diff["discount_term_raw"] = FieldDiff(
            existing=existing_raw,
            incoming=incoming_discount_term_raw,
        )

    # --- approval_threshold ---
    ex_thresh = existing.approval_threshold
    in_thresh = incoming.approval_threshold
    if ex_thresh is None and in_thresh is None:
        pass  # both absent — equal
    elif ex_thresh is None or in_thresh is None:
        diff["approval_threshold"] = FieldDiff(existing=ex_thresh, incoming=in_thresh)
    elif not _decimal_eq(ex_thresh, in_thresh):
        diff["approval_threshold"] = FieldDiff(existing=ex_thresh, incoming=in_thresh)

    # --- notes ---
    if (existing.notes or "") != (incoming.notes or ""):
        diff["notes"] = FieldDiff(existing=existing.notes, incoming=incoming.notes)

    # --- line items ---
    existing_by_line = {li.line_number: li for li in existing.line_items}
    incoming_by_line = {li.line_number: li for li in incoming.line_items}
    all_line_numbers = sorted(set(existing_by_line) | set(incoming_by_line))

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

    return diff


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_contract_by_reference(
    session: Session, contract_reference: str
) -> ContractCreate | None:
    """
    Look up a Contract by its unique contract_reference, eagerly loading line items.

    Args:
        session:            Active SQLAlchemy Session.
        contract_reference: The contract reference string to search for.

    Returns:
        ContractCreate populated from the ORM row and its line items, or None
        if no matching contract exists.

    Notes:
        - Line items are loaded via selectinload to avoid N+1 queries.
        - Returns None on no match; callers treat this as FR-2.6 CONTRACT_NOT_FOUND.
    """
    row: ContractORM | None = (
        session.query(ContractORM)
        .options(selectinload(ContractORM.line_items))
        .filter(ContractORM.contract_reference == contract_reference)
        .first()
    )
    if row is None:
        return None
    return _orm_to_create(row)


def upsert_contract(
    session: Session,
    contract: ContractCreate,
    *,
    discount_term_raw: str | None = None,
) -> UpsertResult:
    """
    Insert or compare a Contract identified by contract.contract_reference.

    Behaviour
    ---------
    1. If no row exists for contract.contract_reference:
       - INSERT the row (header + line items) inside the caller's transaction.
       - Return UpsertCreated with the freshly mapped record.

    2. If a row exists and ALL compared fields are identical:
       - Return UpsertUnchanged.  No write is performed.

    3. If a row exists and one or more compared fields differ:
       - Return UpsertConflict with the *existing* record and a field-level
         diff (dict[str, FieldDiff]).  No write is performed.

    Compared fields
    ---------------
    Header:  discount_term_raw, approval_threshold, notes
    Lines:   unit_price and description per line_number; structural line
             differences (extra/missing lines) are also conflicts.

    vendor_id is NOT compared.

    Args:
        session:           Active SQLAlchemy Session.  The caller is responsible
                           for commit/rollback.
        contract:          Fully validated ContractCreate to insert or compare.
        discount_term_raw: The raw discount term string from the upload source
                           (e.g. ContractExtractionSuccess.discount_term_raw).
                           When None the value is taken from
                           contract.discount_term.discount_term_raw if present.
                           Used for conflict detection and storage.

    Returns:
        UpsertCreated | UpsertUnchanged | UpsertConflict
    """
    # Resolve the raw string to use for comparison and storage.
    raw_for_compare: str | None = discount_term_raw
    if raw_for_compare is None and contract.discount_term is not None:
        raw_for_compare = contract.discount_term.discount_term_raw

    existing_row: ContractORM | None = (
        session.query(ContractORM)
        .options(selectinload(ContractORM.line_items))
        .filter(ContractORM.contract_reference == contract.contract_reference)
        .first()
    )

    # ------------------------------------------------------------------ #
    # Case 1: no existing row — INSERT                                    #
    # ------------------------------------------------------------------ #
    if existing_row is None:
        dt = contract.discount_term
        new_row = ContractORM(
            contract_reference=contract.contract_reference,
            vendor_id=contract.vendor_id,
            discount_term_raw=raw_for_compare,
            discount_pct=str(dt.discount_pct) if dt else None,
            discount_days=dt.discount_days if dt else None,
            net_days=dt.net_days if dt else None,
            approval_threshold=(
                str(contract.approval_threshold)
                if contract.approval_threshold is not None
                else None
            ),
            notes=contract.notes,
            line_items=[
                ContractLineItemORM(
                    line_number=li.line_number,
                    description=li.description,
                    unit_price=str(li.unit_price),
                )
                for li in contract.line_items
            ],
        )
        session.add(new_row)
        session.flush()
        # Re-query to get eager-loaded line_items after flush.
        created_row: ContractORM = (
            session.query(ContractORM)
            .options(selectinload(ContractORM.line_items))
            .filter(ContractORM.contract_reference == contract.contract_reference)
            .one()
        )
        return UpsertCreated(record=_orm_to_create(created_row))

    # ------------------------------------------------------------------ #
    # Case 2 / 3: existing row — compare                                  #
    # ------------------------------------------------------------------ #
    existing_schema = _orm_to_create(existing_row)
    diff = _diff_contract(existing_schema, contract, raw_for_compare)

    if not diff:
        return UpsertUnchanged(record=existing_schema)

    return UpsertConflict(record=existing_schema, diff=diff)
