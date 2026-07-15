"""
tests/unit/test_repositories.py — unit tests for the repositories layer.

Each test class covers one repository module.  Tests follow the same pattern:

1. Build a fresh in-memory SQLite engine and create all tables via
   Base.metadata.create_all (no temp file needed — :memory: is faster and
   leaves nothing on disk).
2. Seed exactly one row (plus required FK rows) in a write session.
3. Assert the *hit* path: the query function returns the correct Pydantic
   schema with all fields matching the seeded data.
4. Assert the *miss* path: querying for a non-existent key returns None.

Isolation: every test method gets its own in-memory database via the
`db_session` fixture, so no state leaks between tests.

No mocking is used — the repositories execute real SQL against SQLite.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from models.base import Base
from models.vendor import VendorORM
from models.purchase_order import PurchaseOrderORM, POLineItemORM
from models.contract import ContractORM, ContractLineItemORM

from repositories.vendor_repo import get_vendor_by_code
from repositories.po_repo import get_po_by_number
from repositories.contract_repo import get_contract_by_reference


# ---------------------------------------------------------------------------
# Shared fixture: fresh in-memory SQLite session per test
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session() -> Session:
    """
    Yield a SQLAlchemy Session backed by an in-memory SQLite database.

    All tables are created fresh for each test; the database is discarded
    when the test ends.  Using :memory: means no filesystem cleanup is needed
    and tests run at maximum speed.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Helpers for seeding ORM rows
# ---------------------------------------------------------------------------

def _seed_vendor(
    session: Session,
    *,
    vendor_code: str = "ACME-001",
    name: str = "Acme Corp",
    contact_email: str | None = "ap@acme.example.com",
    is_active: bool = True,
    notes: str | None = "Preferred supplier",
) -> VendorORM:
    vendor = VendorORM(
        vendor_code=vendor_code,
        name=name,
        contact_email=contact_email,
        is_active=is_active,
        notes=notes,
    )
    session.add(vendor)
    session.commit()
    session.refresh(vendor)
    return vendor


def _seed_po(
    session: Session,
    vendor_id: str,
    *,
    po_number: str = "PO-2024-001",
    po_total: str = "5000.00",
    approval_threshold: str = "10000.00",
    notes: str | None = "Standard order",
    line_items: list[dict] | None = None,
) -> PurchaseOrderORM:
    if line_items is None:
        line_items = [
            {"line_number": 1, "description": "Widget A", "qty": "10", "unit_price": "250.00"},
            {"line_number": 2, "description": "Widget B", "qty": "20", "unit_price": "125.00"},
        ]
    po = PurchaseOrderORM(
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
    session.add(po)
    session.commit()
    session.refresh(po)
    return po


def _seed_contract(
    session: Session,
    vendor_id: str,
    *,
    contract_reference: str = "CTR-2024-ACME",
    discount_term_raw: str | None = "2/10 net 30",
    discount_pct: str | None = "0.02",
    discount_days: int | None = 10,
    net_days: int | None = 30,
    approval_threshold: str | None = "15000.00",
    notes: str | None = "Annual framework",
    line_items: list[dict] | None = None,
) -> ContractORM:
    if line_items is None:
        line_items = [
            {"line_number": 1, "description": "Widget A", "unit_price": "240.00"},
        ]
    contract = ContractORM(
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
    session.add(contract)
    session.commit()
    session.refresh(contract)
    return contract


# ---------------------------------------------------------------------------
# TestVendorRepo
# ---------------------------------------------------------------------------

class TestVendorRepo:
    """get_vendor_by_code() hit and miss paths."""

    def test_hit_returns_vendor_create(self, db_session: Session) -> None:
        """Query for an existing vendor_code returns a populated VendorCreate."""
        _seed_vendor(db_session)

        result = get_vendor_by_code(db_session, "ACME-001")

        assert result is not None
        assert result.vendor_code == "ACME-001"
        assert result.name == "Acme Corp"
        assert result.contact_email == "ap@acme.example.com"
        assert result.is_active is True
        assert result.notes == "Preferred supplier"

    def test_miss_returns_none(self, db_session: Session) -> None:
        """Query for a vendor_code that does not exist returns None."""
        _seed_vendor(db_session, vendor_code="ACME-001")

        result = get_vendor_by_code(db_session, "DOES-NOT-EXIST")

        assert result is None

    def test_hit_inactive_vendor_is_still_returned(self, db_session: Session) -> None:
        """
        The repo returns whatever is in the database, including inactive vendors.
        The caller (matching layer, FR-2.5) decides whether is_active=False is
        an exception — the repo does not filter.
        """
        _seed_vendor(db_session, vendor_code="INACTIVE-001", name="Gone Corp", is_active=False)

        result = get_vendor_by_code(db_session, "INACTIVE-001")

        assert result is not None
        assert result.is_active is False

    def test_hit_vendor_with_no_email_or_notes(self, db_session: Session) -> None:
        """Optional fields that are NULL in the DB map to None in the schema."""
        _seed_vendor(
            db_session,
            vendor_code="MINIMAL-001",
            name="Minimal Vendor",
            contact_email=None,
            notes=None,
        )

        result = get_vendor_by_code(db_session, "MINIMAL-001")

        assert result is not None
        assert result.contact_email is None
        assert result.notes is None

    def test_miss_empty_table(self, db_session: Session) -> None:
        """Query against an empty vendors table returns None (no seeding)."""
        result = get_vendor_by_code(db_session, "ACME-001")
        assert result is None

    def test_hit_correct_row_when_multiple_vendors_exist(self, db_session: Session) -> None:
        """When multiple vendors are seeded, only the matching one is returned."""
        _seed_vendor(db_session, vendor_code="VENDOR-A", name="Vendor A")
        _seed_vendor(db_session, vendor_code="VENDOR-B", name="Vendor B")

        result = get_vendor_by_code(db_session, "VENDOR-B")

        assert result is not None
        assert result.vendor_code == "VENDOR-B"
        assert result.name == "Vendor B"


# ---------------------------------------------------------------------------
# TestPoRepo
# ---------------------------------------------------------------------------

class TestPoRepo:
    """get_po_by_number() hit and miss paths."""

    def test_hit_returns_purchase_order_create(self, db_session: Session) -> None:
        """Query for an existing po_number returns a PurchaseOrderCreate with line items."""
        vendor = _seed_vendor(db_session)
        _seed_po(db_session, vendor.id)

        result = get_po_by_number(db_session, "PO-2024-001")

        assert result is not None
        assert result.po_number == "PO-2024-001"
        assert result.vendor_id == vendor.id
        assert result.po_total == Decimal("5000.00")
        assert result.approval_threshold == Decimal("10000.00")
        assert result.notes == "Standard order"

    def test_hit_line_items_present_and_sorted(self, db_session: Session) -> None:
        """Line items are returned sorted by line_number."""
        vendor = _seed_vendor(db_session)
        _seed_po(
            db_session,
            vendor.id,
            line_items=[
                {"line_number": 2, "description": "Widget B", "qty": "20", "unit_price": "125.00"},
                {"line_number": 1, "description": "Widget A", "qty": "10", "unit_price": "250.00"},
            ],
        )

        result = get_po_by_number(db_session, "PO-2024-001")

        assert result is not None
        assert len(result.line_items) == 2
        assert result.line_items[0].line_number == 1
        assert result.line_items[0].description == "Widget A"
        assert result.line_items[0].qty == Decimal("10")
        assert result.line_items[0].unit_price == Decimal("250.00")
        assert result.line_items[1].line_number == 2
        assert result.line_items[1].description == "Widget B"

    def test_miss_returns_none(self, db_session: Session) -> None:
        """Query for a po_number that does not exist returns None."""
        vendor = _seed_vendor(db_session)
        _seed_po(db_session, vendor.id, po_number="PO-2024-001")

        result = get_po_by_number(db_session, "PO-9999-MISSING")

        assert result is None

    def test_miss_empty_table(self, db_session: Session) -> None:
        """Query against an empty purchase_orders table returns None."""
        result = get_po_by_number(db_session, "PO-2024-001")
        assert result is None

    def test_hit_po_with_no_notes(self, db_session: Session) -> None:
        """notes=None in the DB maps to notes=None in the schema."""
        vendor = _seed_vendor(db_session)
        _seed_po(db_session, vendor.id, notes=None)

        result = get_po_by_number(db_session, "PO-2024-001")

        assert result is not None
        assert result.notes is None

    def test_hit_correct_po_when_multiple_exist(self, db_session: Session) -> None:
        """When multiple POs are seeded, the correct one is returned."""
        vendor = _seed_vendor(db_session)
        _seed_po(db_session, vendor.id, po_number="PO-2024-001", po_total="1000.00")
        _seed_po(db_session, vendor.id, po_number="PO-2024-002", po_total="2000.00")

        result = get_po_by_number(db_session, "PO-2024-002")

        assert result is not None
        assert result.po_number == "PO-2024-002"
        assert result.po_total == Decimal("2000.00")

    def test_hit_single_line_item(self, db_session: Session) -> None:
        """A PO with exactly one line item is returned correctly."""
        vendor = _seed_vendor(db_session)
        _seed_po(
            db_session,
            vendor.id,
            line_items=[
                {"line_number": 1, "description": "Only Item", "qty": "5", "unit_price": "100.00"},
            ],
        )

        result = get_po_by_number(db_session, "PO-2024-001")

        assert result is not None
        assert len(result.line_items) == 1
        assert result.line_items[0].description == "Only Item"


# ---------------------------------------------------------------------------
# TestContractRepo
# ---------------------------------------------------------------------------

class TestContractRepo:
    """get_contract_by_reference() hit and miss paths."""

    def test_hit_returns_contract_create(self, db_session: Session) -> None:
        """Query for an existing contract_reference returns a ContractCreate."""
        vendor = _seed_vendor(db_session)
        _seed_contract(db_session, vendor.id)

        result = get_contract_by_reference(db_session, "CTR-2024-ACME")

        assert result is not None
        assert result.contract_reference == "CTR-2024-ACME"
        assert result.vendor_id == vendor.id
        assert result.notes == "Annual framework"

    def test_hit_discount_term_assembled_correctly(self, db_session: Session) -> None:
        """
        The four flat ORM discount columns are assembled into a DiscountTermSchema.
        """
        vendor = _seed_vendor(db_session)
        _seed_contract(
            db_session,
            vendor.id,
            discount_term_raw="2/10 net 30",
            discount_pct="0.02",
            discount_days=10,
            net_days=30,
        )

        result = get_contract_by_reference(db_session, "CTR-2024-ACME")

        assert result is not None
        assert result.discount_term is not None
        assert result.discount_term.discount_term_raw == "2/10 net 30"
        assert result.discount_term.discount_pct == Decimal("0.02")
        assert result.discount_term.discount_days == 10
        assert result.discount_term.net_days == 30

    def test_hit_no_discount_term_when_columns_null(self, db_session: Session) -> None:
        """
        When all four discount columns are NULL, discount_term is None in the result.
        """
        vendor = _seed_vendor(db_session)
        _seed_contract(
            db_session,
            vendor.id,
            discount_term_raw=None,
            discount_pct=None,
            discount_days=None,
            net_days=None,
        )

        result = get_contract_by_reference(db_session, "CTR-2024-ACME")

        assert result is not None
        assert result.discount_term is None

    def test_hit_approval_threshold_mapped(self, db_session: Session) -> None:
        """approval_threshold is mapped correctly from the ORM row."""
        vendor = _seed_vendor(db_session)
        _seed_contract(db_session, vendor.id, approval_threshold="15000.00")

        result = get_contract_by_reference(db_session, "CTR-2024-ACME")

        assert result is not None
        assert result.approval_threshold == Decimal("15000.00")

    def test_hit_approval_threshold_null(self, db_session: Session) -> None:
        """approval_threshold=None (nullable column) maps to None in the schema."""
        vendor = _seed_vendor(db_session)
        _seed_contract(db_session, vendor.id, approval_threshold=None)

        result = get_contract_by_reference(db_session, "CTR-2024-ACME")

        assert result is not None
        assert result.approval_threshold is None

    def test_hit_line_items_present_and_sorted(self, db_session: Session) -> None:
        """Line items are returned sorted by line_number."""
        vendor = _seed_vendor(db_session)
        _seed_contract(
            db_session,
            vendor.id,
            line_items=[
                {"line_number": 3, "description": "Part C", "unit_price": "30.00"},
                {"line_number": 1, "description": "Part A", "unit_price": "10.00"},
                {"line_number": 2, "description": "Part B", "unit_price": "20.00"},
            ],
        )

        result = get_contract_by_reference(db_session, "CTR-2024-ACME")

        assert result is not None
        assert len(result.line_items) == 3
        assert result.line_items[0].line_number == 1
        assert result.line_items[0].description == "Part A"
        assert result.line_items[1].line_number == 2
        assert result.line_items[2].line_number == 3
        assert result.line_items[2].unit_price == Decimal("30.00")

    def test_miss_returns_none(self, db_session: Session) -> None:
        """Query for a contract_reference that does not exist returns None."""
        vendor = _seed_vendor(db_session)
        _seed_contract(db_session, vendor.id, contract_reference="CTR-2024-ACME")

        result = get_contract_by_reference(db_session, "CTR-9999-MISSING")

        assert result is None

    def test_miss_empty_table(self, db_session: Session) -> None:
        """Query against an empty contracts table returns None."""
        result = get_contract_by_reference(db_session, "CTR-2024-ACME")
        assert result is None

    def test_hit_correct_contract_when_multiple_exist(self, db_session: Session) -> None:
        """When multiple contracts are seeded, only the matching one is returned."""
        vendor = _seed_vendor(db_session)
        _seed_contract(db_session, vendor.id, contract_reference="CTR-A")
        _seed_contract(
            db_session,
            vendor.id,
            contract_reference="CTR-B",
            notes="Second contract",
            line_items=[{"line_number": 1, "description": "Item X", "unit_price": "99.00"}],
        )

        result = get_contract_by_reference(db_session, "CTR-B")

        assert result is not None
        assert result.contract_reference == "CTR-B"
        assert result.notes == "Second contract"
