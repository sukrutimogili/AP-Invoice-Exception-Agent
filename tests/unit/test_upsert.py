"""
tests/unit/test_upsert.py — unit tests for upsert_po() and upsert_contract().

All tests use an in-memory SQLite database (no files, no mocking).
Each test method gets a fresh database via the db_session fixture.

Coverage per function
---------------------
upsert_po
  - create-new: row absent → INSERT → UpsertCreated with correct fields
  - unchanged-reupload: identical PO re-submitted → UpsertUnchanged, no write
  - conflict on po_total
  - conflict on line item unit_price (single line)
  - conflict on multiple fields simultaneously (po_total + line price)
  - conflict on approval_threshold
  - conflict on notes
  - conflict: line present in incoming but absent in existing
  - conflict: line present in existing but absent in incoming

upsert_contract
  - create-new: row absent → INSERT → UpsertCreated with correct fields
  - unchanged-reupload: identical contract re-submitted → UpsertUnchanged
  - conflict on discount_term_raw
  - conflict on line item unit_price
  - conflict on approval_threshold
  - conflict on multiple fields simultaneously
  - conflict: discount_term_raw None → non-None
  - conflict: extra line in incoming

Invariants verified on every conflict
  - outcome == "conflict"
  - diff contains exactly the expected keys
  - diff[key].existing and .incoming hold the right values
  - no DB write: a second get_*_by_* call still returns the original row
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from models.base import Base
from models.contract import ContractCreate, ContractLineItemCreate, DiscountTermSchema
from models.purchase_order import POLineItemCreate, PurchaseOrderCreate
from models.vendor import VendorORM

# ORM models (needed for seeding)
from models.contract import ContractORM, ContractLineItemORM
from models.purchase_order import PurchaseOrderORM, POLineItemORM

from repositories.contract_repo import get_contract_by_reference, upsert_contract
from repositories.po_repo import get_po_by_number, upsert_po
from repositories.upsert_result import FieldDiff, UpsertConflict, UpsertCreated, UpsertUnchanged


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session() -> Session:
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
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_vendor(session: Session, vendor_code: str = "VENDOR-001") -> VendorORM:
    v = VendorORM(vendor_code=vendor_code, name="Test Vendor", is_active=True)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _make_po(
    vendor_id: str,
    *,
    po_number: str = "PO-2024-001",
    po_total: str = "5000.00",
    approval_threshold: str = "10000.00",
    notes: str | None = "Standard order",
    lines: list[dict] | None = None,
) -> PurchaseOrderCreate:
    if lines is None:
        lines = [
            {"line_number": 1, "description": "Widget A", "qty": "10", "unit_price": "250.00"},
            {"line_number": 2, "description": "Widget B", "qty": "5",  "unit_price": "100.00"},
        ]
    return PurchaseOrderCreate(
        po_number=po_number,
        vendor_id=vendor_id,
        po_total=po_total,
        approval_threshold=approval_threshold,
        notes=notes,
        line_items=[POLineItemCreate(**li) for li in lines],
    )


def _make_contract(
    vendor_id: str,
    *,
    contract_reference: str = "CTR-2024-ACME",
    discount_term: DiscountTermSchema | None = None,
    discount_term_raw: str | None = "2/10 net 30",
    approval_threshold: str | None = "15000.00",
    notes: str | None = "Annual framework",
    lines: list[dict] | None = None,
) -> tuple[ContractCreate, str | None]:
    """Return (ContractCreate, discount_term_raw_str) ready for upsert_contract."""
    if lines is None:
        lines = [
            {"line_number": 1, "description": "Widget A", "unit_price": "240.00"},
        ]
    if discount_term is None and discount_term_raw is not None:
        discount_term = DiscountTermSchema(
            discount_term_raw=discount_term_raw,
            discount_pct="0.02",
            discount_days=10,
            net_days=30,
        )
    c = ContractCreate(
        contract_reference=contract_reference,
        vendor_id=vendor_id,
        discount_term=discount_term,
        approval_threshold=approval_threshold,
        notes=notes,
        line_items=[ContractLineItemCreate(**li) for li in lines],
    )
    return c, discount_term_raw


# ===========================================================================
# upsert_po tests
# ===========================================================================


class TestUpsertPOCreateNew:
    def test_returns_upsert_created(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        po = _make_po(vendor.id)

        result = upsert_po(db_session, po)

        assert isinstance(result, UpsertCreated)
        assert result.outcome == "created"

    def test_record_fields_match_incoming(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        po = _make_po(vendor.id)

        result = upsert_po(db_session, po)

        assert isinstance(result, UpsertCreated)
        assert result.record.po_number == "PO-2024-001"
        assert result.record.po_total == Decimal("5000.00")
        assert result.record.approval_threshold == Decimal("10000.00")
        assert result.record.notes == "Standard order"

    def test_line_items_written_and_sorted(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        po = _make_po(vendor.id)

        result = upsert_po(db_session, po)

        assert isinstance(result, UpsertCreated)
        assert len(result.record.line_items) == 2
        assert result.record.line_items[0].line_number == 1
        assert result.record.line_items[0].unit_price == Decimal("250.00")
        assert result.record.line_items[1].line_number == 2

    def test_row_retrievable_after_create(self, db_session: Session) -> None:
        """Inserted row is visible to get_po_by_number in the same session."""
        vendor = _seed_vendor(db_session)
        po = _make_po(vendor.id)
        upsert_po(db_session, po)
        db_session.commit()

        fetched = get_po_by_number(db_session, "PO-2024-001")
        assert fetched is not None
        assert fetched.po_number == "PO-2024-001"


class TestUpsertPOUnchanged:
    def test_returns_upsert_unchanged(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        po = _make_po(vendor.id)
        upsert_po(db_session, po)
        db_session.commit()

        # Re-submit the identical PO.
        result = upsert_po(db_session, po)

        assert isinstance(result, UpsertUnchanged)
        assert result.outcome == "unchanged"

    def test_unchanged_record_matches_stored(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        po = _make_po(vendor.id)
        upsert_po(db_session, po)
        db_session.commit()

        result = upsert_po(db_session, po)

        assert isinstance(result, UpsertUnchanged)
        assert result.record.po_number == "PO-2024-001"
        assert result.record.po_total == Decimal("5000.00")

    def test_no_additional_row_written(self, db_session: Session) -> None:
        """Unchanged re-upload must not insert a duplicate row."""
        from sqlalchemy import func
        vendor = _seed_vendor(db_session)
        po = _make_po(vendor.id)
        upsert_po(db_session, po)
        db_session.commit()

        upsert_po(db_session, po)
        db_session.commit()

        count = db_session.query(func.count(PurchaseOrderORM.id)).scalar()
        assert count == 1

    def test_notes_none_treated_as_empty_for_comparison(self, db_session: Session) -> None:
        """notes=None and notes=None should not generate a conflict."""
        vendor = _seed_vendor(db_session)
        po = _make_po(vendor.id, notes=None)
        upsert_po(db_session, po)
        db_session.commit()

        result = upsert_po(db_session, _make_po(vendor.id, notes=None))
        assert isinstance(result, UpsertUnchanged)


class TestUpsertPOConflict:
    def _create_base_po(self, db_session: Session) -> tuple[VendorORM, PurchaseOrderCreate]:
        vendor = _seed_vendor(db_session)
        po = _make_po(vendor.id)
        upsert_po(db_session, po)
        db_session.commit()
        return vendor, po

    def test_conflict_on_po_total(self, db_session: Session) -> None:
        vendor, _ = self._create_base_po(db_session)
        incoming = _make_po(vendor.id, po_total="9999.00")

        result = upsert_po(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert result.outcome == "conflict"
        assert "po_total" in result.diff
        assert result.diff["po_total"].existing == Decimal("5000.00")
        assert result.diff["po_total"].incoming == Decimal("9999.00")

    def test_conflict_on_approval_threshold(self, db_session: Session) -> None:
        vendor, _ = self._create_base_po(db_session)
        incoming = _make_po(vendor.id, approval_threshold="20000.00")

        result = upsert_po(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert "approval_threshold" in result.diff
        assert result.diff["approval_threshold"].existing == Decimal("10000.00")
        assert result.diff["approval_threshold"].incoming == Decimal("20000.00")

    def test_conflict_on_notes(self, db_session: Session) -> None:
        vendor, _ = self._create_base_po(db_session)
        incoming = _make_po(vendor.id, notes="Changed notes")

        result = upsert_po(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert "notes" in result.diff
        assert result.diff["notes"].existing == "Standard order"
        assert result.diff["notes"].incoming == "Changed notes"

    def test_conflict_on_line_unit_price(self, db_session: Session) -> None:
        vendor, _ = self._create_base_po(db_session)
        incoming = _make_po(
            vendor.id,
            lines=[
                {"line_number": 1, "description": "Widget A", "qty": "10", "unit_price": "300.00"},
                {"line_number": 2, "description": "Widget B", "qty": "5",  "unit_price": "100.00"},
            ],
        )

        result = upsert_po(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert "line_items[1].unit_price" in result.diff
        assert result.diff["line_items[1].unit_price"].existing == Decimal("250.00")
        assert result.diff["line_items[1].unit_price"].incoming == Decimal("300.00")
        # Line 2 is unchanged — should NOT appear in diff.
        assert "line_items[2].unit_price" not in result.diff

    def test_conflict_multiple_fields(self, db_session: Session) -> None:
        """Both po_total and a line price differ — both appear in diff."""
        vendor, _ = self._create_base_po(db_session)
        incoming = _make_po(
            vendor.id,
            po_total="6000.00",
            lines=[
                {"line_number": 1, "description": "Widget A", "qty": "10", "unit_price": "350.00"},
                {"line_number": 2, "description": "Widget B", "qty": "5",  "unit_price": "100.00"},
            ],
        )

        result = upsert_po(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert "po_total" in result.diff
        assert "line_items[1].unit_price" in result.diff
        assert len(result.diff) == 2

    def test_conflict_extra_line_in_incoming(self, db_session: Session) -> None:
        """Incoming has a line that existing does not — reported as conflict."""
        vendor, _ = self._create_base_po(db_session)
        incoming = _make_po(
            vendor.id,
            lines=[
                {"line_number": 1, "description": "Widget A", "qty": "10", "unit_price": "250.00"},
                {"line_number": 2, "description": "Widget B", "qty": "5",  "unit_price": "100.00"},
                {"line_number": 3, "description": "Widget C", "qty": "2",  "unit_price": "50.00"},
            ],
        )

        result = upsert_po(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert "line_items[3].unit_price" in result.diff
        assert result.diff["line_items[3].unit_price"].existing is None
        assert result.diff["line_items[3].unit_price"].incoming == Decimal("50.00")

    def test_conflict_missing_line_in_incoming(self, db_session: Session) -> None:
        """Incoming is missing a line that existing has — reported as conflict."""
        vendor, _ = self._create_base_po(db_session)
        incoming = _make_po(
            vendor.id,
            lines=[
                {"line_number": 1, "description": "Widget A", "qty": "10", "unit_price": "250.00"},
            ],
        )

        result = upsert_po(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert "line_items[2].unit_price" in result.diff
        assert result.diff["line_items[2].unit_price"].existing == Decimal("100.00")
        assert result.diff["line_items[2].unit_price"].incoming is None

    def test_conflict_does_not_write(self, db_session: Session) -> None:
        """On conflict, the original row is preserved unchanged."""
        vendor, _ = self._create_base_po(db_session)
        incoming = _make_po(vendor.id, po_total="99999.00")
        upsert_po(db_session, incoming)
        db_session.commit()

        stored = get_po_by_number(db_session, "PO-2024-001")
        assert stored is not None
        assert stored.po_total == Decimal("5000.00")  # original, not incoming

    def test_conflict_record_is_existing_not_incoming(self, db_session: Session) -> None:
        """result.record on a conflict is the stored row, not the incoming upload."""
        vendor, _ = self._create_base_po(db_session)
        incoming = _make_po(vendor.id, po_total="99999.00")

        result = upsert_po(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        # record must reflect existing DB value
        assert result.record.po_total == Decimal("5000.00")
        # The incoming value is in the diff, not in record
        assert result.diff["po_total"].incoming == Decimal("99999.00")


# ===========================================================================
# upsert_contract tests
# ===========================================================================


class TestUpsertContractCreateNew:
    def test_returns_upsert_created(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        contract, raw = _make_contract(vendor.id)

        result = upsert_contract(db_session, contract, discount_term_raw=raw)

        assert isinstance(result, UpsertCreated)
        assert result.outcome == "created"

    def test_record_fields_match_incoming(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        contract, raw = _make_contract(vendor.id)

        result = upsert_contract(db_session, contract, discount_term_raw=raw)

        assert isinstance(result, UpsertCreated)
        assert result.record.contract_reference == "CTR-2024-ACME"
        assert result.record.approval_threshold == Decimal("15000.00")
        assert result.record.notes == "Annual framework"

    def test_discount_term_raw_stored(self, db_session: Session) -> None:
        """discount_term_raw is persisted and readable via get_contract_by_reference."""
        vendor = _seed_vendor(db_session)
        contract, raw = _make_contract(vendor.id, discount_term_raw="2/10 net 30")
        upsert_contract(db_session, contract, discount_term_raw=raw)
        db_session.commit()

        fetched = get_contract_by_reference(db_session, "CTR-2024-ACME")
        assert fetched is not None
        assert fetched.discount_term is not None
        assert fetched.discount_term.discount_term_raw == "2/10 net 30"

    def test_line_items_written_and_sorted(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        contract, raw = _make_contract(
            vendor.id,
            lines=[
                {"line_number": 2, "description": "Part B", "unit_price": "20.00"},
                {"line_number": 1, "description": "Part A", "unit_price": "10.00"},
            ],
        )
        result = upsert_contract(db_session, contract, discount_term_raw=raw)

        assert isinstance(result, UpsertCreated)
        assert result.record.line_items[0].line_number == 1
        assert result.record.line_items[1].line_number == 2

    def test_no_discount_term_creates_ok(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        contract, raw = _make_contract(vendor.id, discount_term=None, discount_term_raw=None)

        result = upsert_contract(db_session, contract, discount_term_raw=None)

        assert isinstance(result, UpsertCreated)
        assert result.record.discount_term is None

    def test_row_retrievable_after_create(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        contract, raw = _make_contract(vendor.id)
        upsert_contract(db_session, contract, discount_term_raw=raw)
        db_session.commit()

        fetched = get_contract_by_reference(db_session, "CTR-2024-ACME")
        assert fetched is not None


class TestUpsertContractUnchanged:
    def test_returns_upsert_unchanged(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        contract, raw = _make_contract(vendor.id)
        upsert_contract(db_session, contract, discount_term_raw=raw)
        db_session.commit()

        result = upsert_contract(db_session, contract, discount_term_raw=raw)

        assert isinstance(result, UpsertUnchanged)
        assert result.outcome == "unchanged"

    def test_unchanged_record_has_correct_reference(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        contract, raw = _make_contract(vendor.id)
        upsert_contract(db_session, contract, discount_term_raw=raw)
        db_session.commit()

        result = upsert_contract(db_session, contract, discount_term_raw=raw)

        assert isinstance(result, UpsertUnchanged)
        assert result.record.contract_reference == "CTR-2024-ACME"

    def test_no_additional_row_written(self, db_session: Session) -> None:
        from sqlalchemy import func
        vendor = _seed_vendor(db_session)
        contract, raw = _make_contract(vendor.id)
        upsert_contract(db_session, contract, discount_term_raw=raw)
        db_session.commit()

        upsert_contract(db_session, contract, discount_term_raw=raw)
        db_session.commit()

        count = db_session.query(func.count(ContractORM.id)).scalar()
        assert count == 1

    def test_no_discount_term_unchanged(self, db_session: Session) -> None:
        vendor = _seed_vendor(db_session)
        contract, raw = _make_contract(vendor.id, discount_term=None, discount_term_raw=None)
        upsert_contract(db_session, contract, discount_term_raw=None)
        db_session.commit()

        result = upsert_contract(db_session, contract, discount_term_raw=None)
        assert isinstance(result, UpsertUnchanged)


class TestUpsertContractConflict:
    def _create_base_contract(
        self, db_session: Session
    ) -> tuple[VendorORM, ContractCreate, str | None]:
        vendor = _seed_vendor(db_session)
        contract, raw = _make_contract(vendor.id)
        upsert_contract(db_session, contract, discount_term_raw=raw)
        db_session.commit()
        return vendor, contract, raw

    def test_conflict_on_discount_term_raw(self, db_session: Session) -> None:
        vendor, _, _ = self._create_base_contract(db_session)
        new_raw = "1/15 net 45"
        incoming, _ = _make_contract(
            vendor.id,
            discount_term=DiscountTermSchema(
                discount_term_raw=new_raw,
                discount_pct="0.01",
                discount_days=15,
                net_days=45,
            ),
            discount_term_raw=new_raw,
        )

        result = upsert_contract(db_session, incoming, discount_term_raw=new_raw)

        assert isinstance(result, UpsertConflict)
        assert result.outcome == "conflict"
        assert "discount_term_raw" in result.diff
        assert result.diff["discount_term_raw"].existing == "2/10 net 30"
        assert result.diff["discount_term_raw"].incoming == "1/15 net 45"

    def test_conflict_on_approval_threshold(self, db_session: Session) -> None:
        vendor, _, _ = self._create_base_contract(db_session)
        incoming, raw = _make_contract(vendor.id, approval_threshold="99000.00")

        result = upsert_contract(db_session, incoming, discount_term_raw=raw)

        assert isinstance(result, UpsertConflict)
        assert "approval_threshold" in result.diff
        assert result.diff["approval_threshold"].existing == Decimal("15000.00")
        assert result.diff["approval_threshold"].incoming == Decimal("99000.00")

    def test_conflict_on_notes(self, db_session: Session) -> None:
        vendor, _, _ = self._create_base_contract(db_session)
        incoming, raw = _make_contract(vendor.id, notes="Updated terms")

        result = upsert_contract(db_session, incoming, discount_term_raw=raw)

        assert isinstance(result, UpsertConflict)
        assert "notes" in result.diff
        assert result.diff["notes"].existing == "Annual framework"
        assert result.diff["notes"].incoming == "Updated terms"

    def test_conflict_on_line_unit_price(self, db_session: Session) -> None:
        vendor, _, _ = self._create_base_contract(db_session)
        incoming, raw = _make_contract(
            vendor.id,
            lines=[{"line_number": 1, "description": "Widget A", "unit_price": "999.00"}],
        )

        result = upsert_contract(db_session, incoming, discount_term_raw=raw)

        assert isinstance(result, UpsertConflict)
        assert "line_items[1].unit_price" in result.diff
        assert result.diff["line_items[1].unit_price"].existing == Decimal("240.00")
        assert result.diff["line_items[1].unit_price"].incoming == Decimal("999.00")

    def test_conflict_multiple_fields(self, db_session: Session) -> None:
        """discount_term_raw and a line price both differ."""
        vendor, _, _ = self._create_base_contract(db_session)
        new_raw = "0.5/5 net 20"
        incoming, _ = _make_contract(
            vendor.id,
            discount_term=DiscountTermSchema(
                discount_term_raw=new_raw,
                discount_pct="0.005",
                discount_days=5,
                net_days=20,
            ),
            discount_term_raw=new_raw,
            lines=[{"line_number": 1, "description": "Widget A", "unit_price": "500.00"}],
        )

        result = upsert_contract(db_session, incoming, discount_term_raw=new_raw)

        assert isinstance(result, UpsertConflict)
        assert "discount_term_raw" in result.diff
        assert "line_items[1].unit_price" in result.diff

    def test_conflict_discount_term_raw_none_to_value(self, db_session: Session) -> None:
        """Existing has no discount term; incoming supplies one."""
        vendor = _seed_vendor(db_session)
        no_disc, _ = _make_contract(vendor.id, discount_term=None, discount_term_raw=None)
        upsert_contract(db_session, no_disc, discount_term_raw=None)
        db_session.commit()

        incoming, new_raw = _make_contract(vendor.id, discount_term_raw="2/10 net 30")
        result = upsert_contract(db_session, incoming, discount_term_raw=new_raw)

        assert isinstance(result, UpsertConflict)
        assert "discount_term_raw" in result.diff
        assert result.diff["discount_term_raw"].existing is None
        assert result.diff["discount_term_raw"].incoming == "2/10 net 30"

    def test_conflict_extra_line_in_incoming(self, db_session: Session) -> None:
        vendor, _, _ = self._create_base_contract(db_session)
        incoming, raw = _make_contract(
            vendor.id,
            lines=[
                {"line_number": 1, "description": "Widget A", "unit_price": "240.00"},
                {"line_number": 2, "description": "Widget B", "unit_price": "120.00"},
            ],
        )

        result = upsert_contract(db_session, incoming, discount_term_raw=raw)

        assert isinstance(result, UpsertConflict)
        assert "line_items[2].unit_price" in result.diff
        assert result.diff["line_items[2].unit_price"].existing is None
        assert result.diff["line_items[2].unit_price"].incoming == Decimal("120.00")

    def test_conflict_does_not_write(self, db_session: Session) -> None:
        """Original row is unchanged after a conflict is returned."""
        vendor, _, _ = self._create_base_contract(db_session)
        incoming, raw = _make_contract(vendor.id, approval_threshold="1.00")
        upsert_contract(db_session, incoming, discount_term_raw=raw)
        db_session.commit()

        stored = get_contract_by_reference(db_session, "CTR-2024-ACME")
        assert stored is not None
        assert stored.approval_threshold == Decimal("15000.00")

    def test_conflict_record_is_existing_not_incoming(self, db_session: Session) -> None:
        vendor, _, _ = self._create_base_contract(db_session)
        incoming, raw = _make_contract(vendor.id, approval_threshold="1.00")

        result = upsert_contract(db_session, incoming, discount_term_raw=raw)

        assert isinstance(result, UpsertConflict)
        assert result.record.approval_threshold == Decimal("15000.00")
        assert result.diff["approval_threshold"].incoming == Decimal("1.00")



# ===========================================================================
# upsert_vendor tests
# ===========================================================================

from repositories.vendor_repo import upsert_vendor
from models.vendor import VendorCreate


def _make_vendor(**overrides) -> VendorCreate:
    base = dict(
        vendor_code="VENDOR-UV-001",
        name="Unit Test Vendor",
        contact_email="ap@utvendor.example.com",
        is_active=True,
        notes="Test notes",
    )
    base.update(overrides)
    return VendorCreate(**base)


class TestUpsertVendorCreateNew:
    def test_returns_upsert_created(self, db_session: Session) -> None:
        result = upsert_vendor(db_session, _make_vendor())
        assert isinstance(result, UpsertCreated)
        assert result.outcome == "created"

    def test_record_fields_match_incoming(self, db_session: Session) -> None:
        v = _make_vendor()
        result = upsert_vendor(db_session, v)
        assert isinstance(result, UpsertCreated)
        assert result.record.vendor_code == "VENDOR-UV-001"
        assert result.record.name == "Unit Test Vendor"
        assert result.record.contact_email == "ap@utvendor.example.com"
        assert result.record.is_active is True
        assert result.record.notes == "Test notes"

    def test_row_retrievable_after_create(self, db_session: Session) -> None:
        upsert_vendor(db_session, _make_vendor())
        db_session.commit()
        from repositories.vendor_repo import get_vendor_by_code
        fetched = get_vendor_by_code(db_session, "VENDOR-UV-001")
        assert fetched is not None
        assert fetched.name == "Unit Test Vendor"

    def test_minimal_vendor_no_email_no_notes(self, db_session: Session) -> None:
        v = _make_vendor(contact_email=None, notes=None)
        result = upsert_vendor(db_session, v)
        assert isinstance(result, UpsertCreated)
        assert result.record.contact_email is None
        assert result.record.notes is None

    def test_inactive_vendor_created(self, db_session: Session) -> None:
        v = _make_vendor(is_active=False)
        result = upsert_vendor(db_session, v)
        assert isinstance(result, UpsertCreated)
        assert result.record.is_active is False


class TestUpsertVendorUnchanged:
    def test_returns_upsert_unchanged(self, db_session: Session) -> None:
        v = _make_vendor()
        upsert_vendor(db_session, v)
        db_session.commit()

        result = upsert_vendor(db_session, v)
        assert isinstance(result, UpsertUnchanged)
        assert result.outcome == "unchanged"

    def test_unchanged_record_has_correct_vendor_code(self, db_session: Session) -> None:
        v = _make_vendor()
        upsert_vendor(db_session, v)
        db_session.commit()

        result = upsert_vendor(db_session, v)
        assert isinstance(result, UpsertUnchanged)
        assert result.record.vendor_code == "VENDOR-UV-001"

    def test_no_additional_row_written(self, db_session: Session) -> None:
        from sqlalchemy import func
        from models.vendor import VendorORM
        v = _make_vendor()
        upsert_vendor(db_session, v)
        db_session.commit()
        upsert_vendor(db_session, v)
        db_session.commit()
        count = db_session.query(func.count(VendorORM.id)).scalar()
        assert count == 1

    def test_none_email_and_none_notes_unchanged(self, db_session: Session) -> None:
        """None contact_email and None notes should not produce a conflict."""
        v = _make_vendor(contact_email=None, notes=None)
        upsert_vendor(db_session, v)
        db_session.commit()
        result = upsert_vendor(db_session, _make_vendor(contact_email=None, notes=None))
        assert isinstance(result, UpsertUnchanged)


class TestUpsertVendorConflict:
    def _create_base_vendor(self, db_session: Session) -> VendorCreate:
        v = _make_vendor()
        upsert_vendor(db_session, v)
        db_session.commit()
        return v

    def test_conflict_on_name(self, db_session: Session) -> None:
        self._create_base_vendor(db_session)
        incoming = _make_vendor(name="Renamed Vendor")

        result = upsert_vendor(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert result.outcome == "conflict"
        assert "name" in result.diff
        assert result.diff["name"].existing == "Unit Test Vendor"
        assert result.diff["name"].incoming == "Renamed Vendor"

    def test_conflict_on_is_active(self, db_session: Session) -> None:
        self._create_base_vendor(db_session)
        incoming = _make_vendor(is_active=False)

        result = upsert_vendor(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert "is_active" in result.diff
        assert result.diff["is_active"].existing is True
        assert result.diff["is_active"].incoming is False

    def test_conflict_on_contact_email(self, db_session: Session) -> None:
        self._create_base_vendor(db_session)
        incoming = _make_vendor(contact_email="new@example.com")

        result = upsert_vendor(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert "contact_email" in result.diff
        assert result.diff["contact_email"].existing == "ap@utvendor.example.com"
        assert result.diff["contact_email"].incoming == "new@example.com"

    def test_conflict_on_notes(self, db_session: Session) -> None:
        self._create_base_vendor(db_session)
        incoming = _make_vendor(notes="Updated notes")

        result = upsert_vendor(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert "notes" in result.diff

    def test_conflict_multiple_fields(self, db_session: Session) -> None:
        """name and is_active both differ — both appear in diff."""
        self._create_base_vendor(db_session)
        incoming = _make_vendor(name="New Name", is_active=False)

        result = upsert_vendor(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert "name" in result.diff
        assert "is_active" in result.diff
        assert len(result.diff) == 2

    def test_conflict_does_not_write(self, db_session: Session) -> None:
        """Original row is preserved unchanged after a conflict."""
        self._create_base_vendor(db_session)
        upsert_vendor(db_session, _make_vendor(name="Intruder Name"))
        db_session.commit()

        from repositories.vendor_repo import get_vendor_by_code
        stored = get_vendor_by_code(db_session, "VENDOR-UV-001")
        assert stored is not None
        assert stored.name == "Unit Test Vendor"

    def test_conflict_record_is_existing_not_incoming(self, db_session: Session) -> None:
        """result.record holds the stored version, not the incoming one."""
        self._create_base_vendor(db_session)
        incoming = _make_vendor(name="Conflict Name")

        result = upsert_vendor(db_session, incoming)

        assert isinstance(result, UpsertConflict)
        assert result.record.name == "Unit Test Vendor"
        assert result.diff["name"].incoming == "Conflict Name"

    def test_none_to_value_email_is_conflict(self, db_session: Session) -> None:
        """Existing has no email; incoming supplies one → conflict."""
        upsert_vendor(db_session, _make_vendor(contact_email=None))
        db_session.commit()

        result = upsert_vendor(db_session, _make_vendor(contact_email="added@example.com"))

        assert isinstance(result, UpsertConflict)
        assert "contact_email" in result.diff
        assert result.diff["contact_email"].existing is None
        assert result.diff["contact_email"].incoming == "added@example.com"
