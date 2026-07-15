"""
repositories/vendor_repo.py — query helpers for the vendor master table.

Phase 9 wiring point: services that currently hold vendors in an in-process
dict should call get_vendor_by_code() with a session from db.session.get_session()
instead.

Public API
----------
get_vendor_by_code(session, vendor_code) -> VendorCreate | None
    Return the approved-vendor record matching vendor_code, mapped to a
    VendorCreate Pydantic schema, or None if no row exists.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from models.vendor import VendorCreate, VendorORM

__all__ = ["get_vendor_by_code"]


def get_vendor_by_code(session: Session, vendor_code: str) -> VendorCreate | None:
    """
    Look up a vendor by its unique vendor_code.

    Args:
        session:     Active SQLAlchemy Session (from db.session.get_session).
        vendor_code: The ERP vendor code to search for (case-sensitive,
                     must match the value stored in the vendors table).

    Returns:
        VendorCreate populated from the ORM row, or None if no row matches.
        The returned schema includes all VendorBase fields:
            vendor_code, name, contact_email, is_active, notes.

    Notes:
        - Does not raise on a missing vendor — callers (e.g. matching layer)
          are responsible for treating None as FR-2.5 VENDOR_NOT_APPROVED.
        - from_attributes=True is set on VendorBase.model_config, so
          model_validate(orm_row) works without an explicit dict conversion.
    """
    row: VendorORM | None = (
        session.query(VendorORM)
        .filter(VendorORM.vendor_code == vendor_code)
        .first()
    )
    if row is None:
        return None
    return VendorCreate.model_validate(row)
