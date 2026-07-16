"""
scripts/seed_db.py — Seed the database with sample vendors, POs, and contracts.

Run from the project root:

    python scripts/seed_db.py

Or with an explicit database URL (overrides .env / DATABASE_URL env var):

    DATABASE_URL=sqlite:///./mydev.db python scripts/seed_db.py

Idempotency
-----------
The script is safe to re-run.  Every record is keyed on its natural business
key (vendor_code, po_number, contract_reference).  If a row already exists it
is left unchanged; no duplicate rows are ever created.  Run it once after
`alembic upgrade head` and again whenever you reset the database.

Seed scenarios
--------------
Four scenarios exercise the full range of matching and exception paths:

  SCENARIO 1 — Clean STP path  (vendor ACME-001 / PO-ACME-001 / CTR-ACME-001)
    All fields match exactly.  An invoice billed at exactly the PO/contract
    values (qty=10, unit_price=250.00, grand_total=2500.00) will pass all
    FR-2 checks and reach the STP path.  Includes a 2/10 net 30 discount term
    so the discount evaluator is also exercised.

  SCENARIO 2 — Price variance exception  (vendor GLOBEX-001 / PO-GLOBEX-001 / CTR-GLOBEX-001)
    Contract unit_price is 100.00 but the PO records 100.00 as well.  Any
    invoice that bills at 105.00+ (>5% variance at default 0% tolerance) will
    fail the prices_match check and be routed as PRICE_VARIANCE exception.

  SCENARIO 3 — Inactive vendor exception  (vendor INACTIVE-001 / PO-INACTIVE-001 / CTR-INACTIVE-001)
    Vendor is marked is_active=False.  Any invoice referencing vendor code
    INACTIVE-001 will fail the vendor_known check (FR-2.5) and be routed as
    UNKNOWN_VENDOR regardless of how well the numbers match.

  SCENARIO 4 — High-value approval required  (vendor BIGCO-001 / PO-BIGCO-001 / CTR-BIGCO-001)
    PO total is 50,000.00 with approval_threshold=10,000.00.  An invoice for
    the full amount without an approval on file will fail the approval_satisfied
    check (FR-2.4) and be routed as MISSING_APPROVAL.  If approval_on_file=True
    is passed to the matching engine the same invoice passes.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make the project root importable when running as `python scripts/seed_db.py`
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy.orm import Session  # noqa: E402  (after sys.path fix)

from db.session import engine, get_session  # noqa: E402
from models.base import Base  # noqa: E402
from models.vendor import VendorORM  # noqa: E402
from models.purchase_order import PurchaseOrderORM, POLineItemORM  # noqa: E402
from models.contract import ContractORM, ContractLineItemORM  # noqa: E402

# Use a module logger for library callers; the _log() helper below also
# prints to stdout so progress is always visible when run as a script,
# regardless of how the root logger was configured by get_settings().
logger = logging.getLogger("seed_db")


def _log(msg: str) -> None:
    """Print to stdout and emit at INFO level for library callers."""
    print(msg)
    logger.info(msg)


# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------

def _get_or_create_vendor(session: Session, **kwargs: object) -> VendorORM:
    """
    Return the existing VendorORM row for kwargs["vendor_code"], or insert a
    new one.  No UPDATE is performed — existing rows are left unchanged.
    """
    vendor_code = str(kwargs["vendor_code"])
    row = session.query(VendorORM).filter_by(vendor_code=vendor_code).first()
    if row is not None:
        _log("  vendor %s — already exists, skipping", vendor_code)
        return row
    row = VendorORM(**kwargs)
    session.add(row)
    session.flush()  # populate row.id before callers reference it as FK
    _log("  vendor %s — created (id=%s)", vendor_code, row.id)
    return row


def _get_or_create_po(
    session: Session,
    vendor_id: str,
    po_number: str,
    po_total: str,
    approval_threshold: str,
    notes: str | None,
    line_items: list[dict],
) -> PurchaseOrderORM:
    """
    Return the existing PurchaseOrderORM row for po_number, or insert a new
    one (with its line items).
    """
    row = session.query(PurchaseOrderORM).filter_by(po_number=po_number).first()
    if row is not None:
        _log("  PO %s — already exists, skipping", po_number)
        return row
    row = PurchaseOrderORM(
        po_number=po_number,
        vendor_id=vendor_id,
        po_total=po_total,
        approval_threshold=approval_threshold,
        notes=notes,
        line_items=[
            POLineItemORM(
                line_number=li["line_number"],
                description=li["description"],
                qty=li["qty"],
                unit_price=li["unit_price"],
            )
            for li in line_items
        ],
    )
    session.add(row)
    session.flush()
    _log("  PO %s — created (id=%s, %d lines)", po_number, row.id, len(line_items))
    return row


def _get_or_create_contract(
    session: Session,
    vendor_id: str,
    contract_reference: str,
    discount_term_raw: str | None,
    discount_pct: str | None,
    discount_days: int | None,
    net_days: int | None,
    approval_threshold: str | None,
    notes: str | None,
    line_items: list[dict],
) -> ContractORM:
    """
    Return the existing ContractORM row for contract_reference, or insert a
    new one (with its line items).
    """
    row = (
        session.query(ContractORM)
        .filter_by(contract_reference=contract_reference)
        .first()
    )
    if row is not None:
        _log("  contract %s — already exists, skipping", contract_reference)
        return row
    row = ContractORM(
        contract_reference=contract_reference,
        vendor_id=vendor_id,
        discount_term_raw=discount_term_raw,
        discount_pct=discount_pct,
        discount_days=discount_days,
        net_days=net_days,
        approval_threshold=approval_threshold,
        notes=notes,
        line_items=[
            ContractLineItemORM(
                line_number=li["line_number"],
                description=li["description"],
                unit_price=li["unit_price"],
            )
            for li in line_items
        ],
    )
    session.add(row)
    session.flush()
    logger.info(
        "  contract %s — created (id=%s, %d lines)",
        contract_reference,
        row.id,
        len(line_items),
    )
    return row


# ---------------------------------------------------------------------------
# Seed scenarios
# ---------------------------------------------------------------------------

def _seed_scenario_1_stp(session: Session) -> None:
    """
    SCENARIO 1 — Clean STP path.

    Vendor ACME-001 is active and approved.
    PO-ACME-001: 1 line, qty=10, unit_price=250.00, po_total=2500.00.
    CTR-ACME-001: matching contracted price=250.00, discount term 2/10 net 30.

    An invoice with line_number=1, qty=10, unit_price=250.00, grand_total=2500.00
    will pass all FR-2 checks and reach the STP path.  The discount evaluator
    will recommend TAKE_DISCOUNT if payment is made within 10 days.
    """
    _log("Scenario 1 — Clean STP path")
    vendor = _get_or_create_vendor(
        session,
        vendor_code="ACME-001",
        name="Acme Supplies Ltd",
        contact_email="accounts@acme.example.com",
        is_active=True,
        notes="Preferred supplier — widget category",
    )
    _get_or_create_po(
        session,
        vendor_id=vendor.id,
        po_number="PO-ACME-001",
        po_total="2500.00",
        approval_threshold="10000.00",
        notes="Standard widget order — Scenario 1 (STP)",
        line_items=[
            {
                "line_number": 1,
                "description": "Widget Model A",
                "qty": "10",
                "unit_price": "250.00",
            },
        ],
    )
    _get_or_create_contract(
        session,
        vendor_id=vendor.id,
        contract_reference="CTR-ACME-001",
        discount_term_raw="2/10 net 30",
        discount_pct="0.02",
        discount_days=10,
        net_days=30,
        approval_threshold=None,
        notes="Annual framework — Scenario 1 (STP)",
        line_items=[
            {
                "line_number": 1,
                "description": "Widget Model A",
                "unit_price": "250.00",
            },
        ],
    )


def _seed_scenario_2_price_variance(session: Session) -> None:
    """
    SCENARIO 2 — Price variance → PRICE_VARIANCE exception.

    Vendor GLOBEX-001 is active.
    PO-GLOBEX-001: 2 lines at contracted prices.
    CTR-GLOBEX-001: contracted unit_price=100.00 (line 1), 200.00 (line 2).

    An invoice that bills line 1 at 110.00 (10% over contract) will fail
    prices_match at the default 0% tolerance and be routed as PRICE_VARIANCE.

    The PO total (3000.00) and qtys are designed to also pass if prices are
    correct — isolating the price check as the single failure mode.
    """
    _log("Scenario 2 — Price variance exception path")
    vendor = _get_or_create_vendor(
        session,
        vendor_code="GLOBEX-001",
        name="Globex Industrial",
        contact_email="ap@globex.example.com",
        is_active=True,
        notes="Scenario 2 — price variance trigger vendor",
    )
    _get_or_create_po(
        session,
        vendor_id=vendor.id,
        po_number="PO-GLOBEX-001",
        po_total="3000.00",
        approval_threshold="10000.00",
        notes="Multi-line order — Scenario 2 (price variance)",
        line_items=[
            {
                "line_number": 1,
                "description": "Component Alpha",
                "qty": "10",
                "unit_price": "100.00",
            },
            {
                "line_number": 2,
                "description": "Component Beta",
                "qty": "10",
                "unit_price": "200.00",
            },
        ],
    )
    _get_or_create_contract(
        session,
        vendor_id=vendor.id,
        contract_reference="CTR-GLOBEX-001",
        # No early-payment discount on this contract
        discount_term_raw=None,
        discount_pct=None,
        discount_days=None,
        net_days=30,
        approval_threshold=None,
        notes="Scenario 2 — contracted prices; bill at 110.00 on line 1 to trigger PRICE_VARIANCE",
        line_items=[
            {
                "line_number": 1,
                "description": "Component Alpha",
                "unit_price": "100.00",  # invoice must match this exactly (0% tolerance default)
            },
            {
                "line_number": 2,
                "description": "Component Beta",
                "unit_price": "200.00",
            },
        ],
    )


def _seed_scenario_3_inactive_vendor(session: Session) -> None:
    """
    SCENARIO 3 — Inactive vendor → UNKNOWN_VENDOR exception (FR-2.5).

    Vendor INACTIVE-001 is present in the database but marked is_active=False.
    PO and contract exist and would otherwise match a clean invoice.

    Any invoice referencing this vendor will fail _check_vendor() regardless
    of how well the numbers match, because is_active=False means the vendor
    is not in the approved vendor master.
    """
    _log("Scenario 3 — Inactive vendor exception path")
    vendor = _get_or_create_vendor(
        session,
        vendor_code="INACTIVE-001",
        name="Defunct Goods Co",
        contact_email=None,
        is_active=False,  # <-- triggers UNKNOWN_VENDOR in the matching engine
        notes="Scenario 3 — deactivated vendor; any invoice fails vendor_known check",
    )
    _get_or_create_po(
        session,
        vendor_id=vendor.id,
        po_number="PO-INACTIVE-001",
        po_total="500.00",
        approval_threshold="10000.00",
        notes="Scenario 3 — PO exists but vendor is inactive",
        line_items=[
            {
                "line_number": 1,
                "description": "Legacy Part",
                "qty": "5",
                "unit_price": "100.00",
            },
        ],
    )
    _get_or_create_contract(
        session,
        vendor_id=vendor.id,
        contract_reference="CTR-INACTIVE-001",
        discount_term_raw=None,
        discount_pct=None,
        discount_days=None,
        net_days=45,
        approval_threshold=None,
        notes="Scenario 3 — contract exists but vendor is inactive",
        line_items=[
            {
                "line_number": 1,
                "description": "Legacy Part",
                "unit_price": "100.00",
            },
        ],
    )


def _seed_scenario_4_approval_required(session: Session) -> None:
    """
    SCENARIO 4 — High-value invoice, approval required (FR-2.4).

    Vendor BIGCO-001 is active.
    PO-BIGCO-001: grand total 50,000.00 — well above the 10,000.00 threshold.
    CTR-BIGCO-001: matching contracted prices.

    An invoice for the full amount with approval_on_file=False will fail
    approval_satisfied (MISSING_APPROVAL exception).

    The same invoice with approval_on_file=True passes all checks → STP.
    This lets you toggle the approval flag in a test to see both outcomes.
    """
    _log("Scenario 4 — High-value approval-required path")
    vendor = _get_or_create_vendor(
        session,
        vendor_code="BIGCO-001",
        name="BigCo Enterprise Solutions",
        contact_email="procurement@bigco.example.com",
        is_active=True,
        notes="Scenario 4 — high-value orders require prior approval",
    )
    _get_or_create_po(
        session,
        vendor_id=vendor.id,
        po_number="PO-BIGCO-001",
        po_total="50000.00",
        approval_threshold="10000.00",  # invoice >= 10k needs an approval on file
        notes="Scenario 4 — high-value PO; approval_on_file=False triggers MISSING_APPROVAL",
        line_items=[
            {
                "line_number": 1,
                "description": "Enterprise Software License",
                "qty": "5",
                "unit_price": "8000.00",
            },
            {
                "line_number": 2,
                "description": "Implementation Services",
                "qty": "10",
                "unit_price": "1000.00",
            },
        ],
    )
    _get_or_create_contract(
        session,
        vendor_id=vendor.id,
        contract_reference="CTR-BIGCO-001",
        discount_term_raw="1/15 net 45",
        discount_pct="0.01",
        discount_days=15,
        net_days=45,
        approval_threshold="10000.00",
        notes="Scenario 4 — contracted prices match PO exactly",
        line_items=[
            {
                "line_number": 1,
                "description": "Enterprise Software License",
                "unit_price": "8000.00",
            },
            {
                "line_number": 2,
                "description": "Implementation Services",
                "unit_price": "1000.00",
            },
        ],
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def seed() -> None:
    """Create all tables (if absent) and insert all seed scenarios."""
    _log("Creating tables if they don't exist...")
    Base.metadata.create_all(bind=engine)

    _log("Seeding sample data...")
    with get_session() as session:
        _seed_scenario_1_stp(session)
        _seed_scenario_2_price_variance(session)
        _seed_scenario_3_inactive_vendor(session)
        _seed_scenario_4_approval_required(session)
        session.commit()

    _log("Done.  Database seeded successfully.")


if __name__ == "__main__":
    seed()
