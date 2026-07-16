# repositories/__init__.py — database query layer.
#
# Each module exposes query/upsert functions that accept a SQLAlchemy Session
# (from db.session.get_session) and return Pydantic *Create schemas or typed
# UpsertResult values.  No FastAPI dependency; usable from any service or test.

from repositories.contract_repo import get_contract_by_reference, upsert_contract
from repositories.po_repo import get_po_by_number, upsert_po
from repositories.upsert_result import (
    FieldDiff,
    UpsertConflict,
    UpsertCreated,
    UpsertResult,
    UpsertUnchanged,
)
from repositories.vendor_repo import get_vendor_by_code, get_vendor_by_id, upsert_vendor

__all__ = [
    # Vendor
    "get_vendor_by_code",
    "get_vendor_by_id",
    "upsert_vendor",
    # PO
    "get_po_by_number",
    "upsert_po",
    # Contract
    "get_contract_by_reference",
    "upsert_contract",
    # Upsert result types
    "FieldDiff",
    "UpsertConflict",
    "UpsertCreated",
    "UpsertResult",
    "UpsertUnchanged",
]
