"""
db/resolver.py — Shared entity resolution helper for the invoice pipeline.

Both callers — ui/components/pipeline_runner.py and api/invoices.py — use
resolve_invoice_entities() so the lookup strategy is defined exactly once.

Resolution strategy
-------------------
The extracted invoice carries three reference strings:
  invoice.po_reference        → used to look up PurchaseOrderORM by po_number
  invoice.contract_reference  → used to look up ContractORM by contract_reference
  invoice.vendor_name         → free-text; NOT used for lookup (see below)

Vendor resolution — why we don't match by name
  InvoiceCreate.vendor_name is extracted free-text (FR-1.4): it may differ in
  capitalisation, include trading-name suffixes, or be ambiguous across
  multiple vendor records.  Name-based fuzzy matching would silently produce
  wrong results and is explicitly flagged as fragile in spec.md §4.

  Instead: once the PO is resolved, PurchaseOrderORM.vendor_id is the UUID PK
  of the vendor — this is always unambiguous.  We call get_vendor_by_id() with
  that FK.  If the PO cannot be resolved, vendor is None (the matching engine
  will raise PO_NOT_FOUND anyway; carrying a guessed vendor would be misleading).

  If a future extraction version surfaces a vendor_code field, replace the
  PO-based lookup with get_vendor_by_code(session, invoice.vendor_code).

Public API
----------
resolve_invoice_entities(session, invoice) -> ResolvedEntities
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from models.contract import ContractCreate
from models.invoice import InvoiceCreate
from models.purchase_order import PurchaseOrderCreate
from models.vendor import VendorCreate
from repositories.contract_repo import get_contract_by_reference
from repositories.po_repo import get_po_by_number
from repositories.vendor_repo import get_vendor_by_id

__all__ = ["ResolvedEntities", "resolve_invoice_entities"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedEntities:
    """
    The three entities resolved from the database for one invoice.

    Any field may be None if the corresponding reference could not be resolved;
    the matching engine treats each None as the appropriate failure reason:
      vendor=None   → UNKNOWN_VENDOR  (FR-2.5)
      po=None       → PO_NOT_FOUND    (FR-2.6)
      contract=None → CONTRACT_NOT_FOUND (FR-2.6)
    """

    vendor: VendorCreate | None
    po: PurchaseOrderCreate | None
    contract: ContractCreate | None


def resolve_invoice_entities(
    session: Session,
    invoice: InvoiceCreate,
) -> ResolvedEntities:
    """
    Resolve vendor, PO, and contract for an extracted invoice from the database.

    Lookup order:
      1. PO by invoice.po_reference  (po_number column)
      2. Contract by invoice.contract_reference  (contract_reference column)
      3. Vendor by PO.vendor_id PK — only if the PO was found

    All lookups return None on a miss rather than raising; the matching engine
    downstream will flag the appropriate exception reason code.

    Args:
        session: Active SQLAlchemy Session (from db.session.get_session).
        invoice: Validated InvoiceCreate from extraction.

    Returns:
        ResolvedEntities(vendor, po, contract) — any may be None.
    """
    # -----------------------------------------------------------------------
    # 1. Resolve PO
    # -----------------------------------------------------------------------
    po = get_po_by_number(session, invoice.po_reference)
    if po is None:
        logger.warning(
            "PO not found in DB",
            extra={"po_reference": invoice.po_reference, "invoice_number": invoice.invoice_number},
        )
    else:
        logger.debug(
            "PO resolved",
            extra={"po_reference": invoice.po_reference},
        )

    # -----------------------------------------------------------------------
    # 2. Resolve contract
    # -----------------------------------------------------------------------
    contract = get_contract_by_reference(session, invoice.contract_reference)
    if contract is None:
        logger.warning(
            "Contract not found in DB",
            extra={
                "contract_reference": invoice.contract_reference,
                "invoice_number": invoice.invoice_number,
            },
        )
    else:
        logger.debug(
            "Contract resolved",
            extra={"contract_reference": invoice.contract_reference},
        )

    # -----------------------------------------------------------------------
    # 3. Resolve vendor via PO.vendor_id (avoids name-based matching)
    #
    # We need the raw vendor_id from the PO ORM row, which is a UUID string.
    # PurchaseOrderCreate carries vendor_id as a plain str field, so we can
    # read it directly from the Pydantic object returned by get_po_by_number().
    # -----------------------------------------------------------------------
    vendor: VendorCreate | None = None
    if po is not None:
        vendor = get_vendor_by_id(session, po.vendor_id)
        if vendor is None:
            # The FK exists in the PO row but the vendor record is missing —
            # this is a data integrity problem (should be caught by the FK
            # constraint, but log it explicitly so it surfaces in monitoring).
            logger.error(
                "Vendor PK referenced by PO not found in vendors table — "
                "possible data integrity issue",
                extra={"vendor_id": po.vendor_id, "po_reference": invoice.po_reference},
            )
        else:
            logger.debug(
                "Vendor resolved via PO FK",
                extra={"vendor_id": po.vendor_id, "vendor_code": vendor.vendor_code},
            )
    else:
        # Cannot resolve vendor without a PO — log at info level only
        # (PO_NOT_FOUND will be the primary exception reason; don't double-warn).
        logger.info(
            "Vendor resolution skipped — PO not found",
            extra={"invoice_number": invoice.invoice_number},
        )

    return ResolvedEntities(vendor=vendor, po=po, contract=contract)
