"""
tests/unit/test_vendor_auto_create.py
──────────────────────────────────────────────────────────────────────────────
Unit / integration tests for the auto-create-vendor behaviour added to
ui/components/pipeline_runner.py::run_extraction_pipeline_with_documents().

Scenarios
─────────
1. Unknown vendor during PO upload
   No vendor row initially exists → vendor is auto-created → PO persists with
   the new vendor UUID → VENDOR_AUTO_CREATED audit event is emitted only after
   session.commit() succeeds.

2. Contract reuses PO's auto-created vendor (same run, same session)
   PO is uploaded first (vendor auto-created), contract for the same vendor
   is uploaded in the same call → contract branch finds the vendor via
   get_vendor_by_code() (post-commit) → no second vendor row is created →
   no second VENDOR_AUTO_CREATED event.

3. Vendor auto-create followed by a PO-level conflict (commit-ordering proof)
   Vendor does not exist, PO extraction succeeds, but upsert_po() returns
   UpsertConflict (po_number already exists with different data) →
   session.commit() is NOT called → vendor row is NOT persisted →
   NO VENDOR_AUTO_CREATED audit event.

4. End-to-end upload chain
   PO uploaded (vendor auto-created), contract uploaded (vendor reused),
   invoice referencing both uploaded → invoice resolves vendor + PO + contract
   successfully, no UNKNOWN_VENDOR / PO_NOT_FOUND / CONTRACT_NOT_FOUND reason
   codes attributable to the vendor having been unknown initially.

Test architecture
─────────────────
- In-memory SQLite via a dedicated db_engine fixture (same pattern as
  test_pipeline_db_integration.py).
- db.session.SessionLocal is patched to point at the test engine so that
  run_extraction_pipeline_with_documents() uses the in-memory DB.
- Extraction agents are patched to return pre-built success objects so no
  network calls are made.
- audit.writer._audit_log is cleared before each test via the autouse
  clear_audit fixture.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import audit.writer as _audit_writer
from models.base import Base
from models.contract import ContractCreate, ContractLineItemCreate, ContractORM, ContractLineItemORM
from models.enums import AuditEventType, ExtractionStatus, InvoiceStatus
from models.invoice import InvoiceCreate, InvoiceLineItemCreate
from models.purchase_order import (
    POLineItemCreate,
    POLineItemORM,
    PurchaseOrderCreate,
    PurchaseOrderORM,
)
from models.vendor import VendorCreate, VendorORM
from extraction.schemas import ExtractionSuccess
from extraction.po_schemas import POExtractionSuccess
from extraction.contract_schemas import ContractExtractionSuccess
from ui.components.pipeline_runner import run_extraction_pipeline_with_documents


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_engine():
    """Fresh in-memory SQLite engine with all tables created."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Session bound to the in-memory engine."""
    SL = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    session = SL()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def clear_audit():
    """Clear the in-process audit log before every test."""
    _audit_writer.clear_audit_log()
    yield
    _audit_writer.clear_audit_log()


def _patch_db(db_engine):
    """Redirect db.session.SessionLocal to the test engine."""
    TestSL = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    return patch("db.session.SessionLocal", TestSL)


# ─────────────────────────────────────────────────────────────────────────────
# Builder helpers
# ─────────────────────────────────────────────────────────────────────────────

_VENDOR_CODE = "AUTO-V1"
_PO_NUMBER = "PO-AUTO-001"
_CONTRACT_REF = "CTR-AUTO-001"
_INV_NUMBER = "INV-AUTO-001"


def _make_po_create(vendor_code: str = _VENDOR_CODE) -> PurchaseOrderCreate:
    """PurchaseOrderCreate whose vendor_id is set to the raw vendor_code
    (as the extraction agent would produce it — caller must replace with UUID)."""
    return PurchaseOrderCreate(
        po_number=_PO_NUMBER,
        vendor_id=vendor_code,          # placeholder — replaced by pipeline
        po_total=Decimal("1000.00"),
        approval_threshold=Decimal("5000.00"),
        notes="Auto-create test PO",
        line_items=[
            POLineItemCreate(
                line_number=1,
                description="Widget A",
                qty=Decimal("10"),
                unit_price=Decimal("100.00"),
            )
        ],
    )


def _make_po_extraction_success(
    vendor_code: str = _VENDOR_CODE,
) -> POExtractionSuccess:
    po = _make_po_create(vendor_code)
    return POExtractionSuccess(
        po=po,
        vendor_code_extracted=vendor_code,
        raw_payload=json.dumps({"mocked": True}),
        attempt_count=1,
    )


def _make_contract_create(vendor_code: str = _VENDOR_CODE) -> ContractCreate:
    return ContractCreate(
        contract_reference=_CONTRACT_REF,
        vendor_id=vendor_code,          # placeholder — replaced by pipeline
        discount_term=None,
        approval_threshold=None,
        notes="Auto-create test contract",
        line_items=[
            ContractLineItemCreate(
                line_number=1,
                description="Widget A",
                unit_price=Decimal("100.00"),
            )
        ],
    )


def _make_contract_extraction_success(
    vendor_code: str = _VENDOR_CODE,
) -> ContractExtractionSuccess:
    contract = _make_contract_create(vendor_code)
    return ContractExtractionSuccess(
        contract=contract,
        vendor_code_extracted=vendor_code,
        discount_term_raw=None,
        raw_payload=json.dumps({"mocked": True}),
        attempt_count=1,
    )


def _make_invoice_create(
    po_reference: str = _PO_NUMBER,
    contract_reference: str = _CONTRACT_REF,
) -> InvoiceCreate:
    today = date.today()
    return InvoiceCreate(
        invoice_number=_INV_NUMBER,
        vendor_name="Auto-create Vendor",
        invoice_date=today - timedelta(days=5),
        po_reference=po_reference,
        contract_reference=contract_reference,
        payment_terms="Net 30",
        subtotal=Decimal("1000.00"),
        tax=Decimal("0.00"),
        grand_total=Decimal("1000.00"),
        due_date=today + timedelta(days=25),
        line_items=[
            InvoiceLineItemCreate(
                line_number=1,
                description="Widget A",
                qty=Decimal("10"),
                unit_price=Decimal("100.00"),
                amount=Decimal("1000.00"),
            )
        ],
        extraction_status=ExtractionStatus.EXTRACTED,
        invoice_status=InvoiceStatus.EXTRACTED,
    )


def _make_invoice_extraction_success(invoice: InvoiceCreate) -> ExtractionSuccess:
    return ExtractionSuccess(
        invoice=invoice,
        raw_payload=json.dumps({"mocked": True}),
        attempt_count=1,
    )


def _seed_po_conflict(session: Session, vendor_id: str) -> None:
    """Seed a PO with the same po_number but DIFFERENT po_total so that
    upsert_po() will return UpsertConflict."""
    po = PurchaseOrderORM(
        po_number=_PO_NUMBER,
        vendor_id=vendor_id,
        po_total="9999.00",            # differs from _make_po_create's 1000.00
        approval_threshold="5000.00",
        notes="Seeded for conflict test",
        line_items=[
            POLineItemORM(
                line_number=1,
                description="Widget A",
                qty="10",
                unit_price="100.00",
            )
        ],
    )
    session.add(po)
    session.commit()


def _vendor_audit_events() -> list[dict]:
    """Return all VENDOR_AUTO_CREATED events from the audit log."""
    return [
        e for e in _audit_writer.get_all_events()
        if e.get("event_type") == AuditEventType.VENDOR_AUTO_CREATED.value
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1 — Unknown vendor during PO upload
# ─────────────────────────────────────────────────────────────────────────────


class TestUnknownVendorPOUpload:
    """
    No vendor exists → vendor is auto-created → PO is persisted with the new
    vendor UUID → VENDOR_AUTO_CREATED audit event emitted after commit.
    """

    def _run(self, db_engine):
        po_success = _make_po_extraction_success()
        invoice = _make_invoice_create()
        inv_success = _make_invoice_extraction_success(invoice)
        with (
            _patch_db(db_engine),
            patch(
                "ui.components.pipeline_runner.PurchaseOrderExtractionAgent.extract",
                return_value=po_success,
            ),
            patch(
                "ui.components.pipeline_runner.ContractExtractionAgent.extract",
                return_value=_make_contract_extraction_success(),
            ),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=inv_success,
            ),
        ):
            return run_extraction_pipeline_with_documents(
                invoice_text="(invoice text — patched)",
                po_text="(po text — patched)",
                contract_text=None,
            )

    def test_vendor_row_created(self, db_session: Session, db_engine) -> None:
        self._run(db_engine)
        # Read vendor rows using a fresh session on the same engine
        SL = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
        with SL() as s:
            row = (
                s.query(VendorORM)
                .filter(VendorORM.vendor_code == _VENDOR_CODE)
                .first()
            )
        assert row is not None, "Vendor row should have been auto-created"
        assert row.is_active is True
        assert row.vendor_code == _VENDOR_CODE

    def test_po_row_created_with_correct_vendor_id(
        self, db_session: Session, db_engine
    ) -> None:
        self._run(db_engine)
        SL = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
        with SL() as s:
            vendor_row = (
                s.query(VendorORM)
                .filter(VendorORM.vendor_code == _VENDOR_CODE)
                .first()
            )
            po_row = (
                s.query(PurchaseOrderORM)
                .filter(PurchaseOrderORM.po_number == _PO_NUMBER)
                .first()
            )
        assert po_row is not None, "PO row should have been persisted"
        assert vendor_row is not None
        assert po_row.vendor_id == vendor_row.id, (
            "PO.vendor_id must be the real UUID, not the raw vendor code"
        )

    def test_vendor_auto_created_audit_event_emitted(
        self, db_session: Session, db_engine
    ) -> None:
        self._run(db_engine)
        events = _vendor_audit_events()
        assert len(events) == 1, f"Expected 1 VENDOR_AUTO_CREATED event, got {len(events)}"

    def test_audit_event_payload_fields(
        self, db_session: Session, db_engine
    ) -> None:
        self._run(db_engine)
        events = _vendor_audit_events()
        assert events, "No VENDOR_AUTO_CREATED event found"
        payload = json.loads(events[0]["payload_json"])
        assert payload["vendor_code"] == _VENDOR_CODE
        assert payload["source_document"] == "PO"
        assert payload["reason"] == "Vendor code not previously known"
        assert "created_vendor_id" in payload
        assert payload["created_vendor_id"]  # non-empty UUID

    def test_audit_vendor_id_matches_db_row(
        self, db_session: Session, db_engine
    ) -> None:
        """The UUID in the audit event must match the actual DB row id."""
        self._run(db_engine)
        events = _vendor_audit_events()
        payload = json.loads(events[0]["payload_json"])
        audit_vendor_id = payload["created_vendor_id"]

        SL = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
        with SL() as s:
            row = (
                s.query(VendorORM)
                .filter(VendorORM.vendor_code == _VENDOR_CODE)
                .first()
            )
        assert row is not None
        assert audit_vendor_id == row.id



# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2 — Contract reuses PO's auto-created vendor (same run)
# ─────────────────────────────────────────────────────────────────────────────


class TestContractReusesAutoCreatedVendor:
    """
    PO and Contract are uploaded together (same call). The PO branch
    auto-creates the vendor and commits. The Contract branch then finds the
    vendor via get_vendor_by_code() and reuses its UUID — no second vendor row,
    no second VENDOR_AUTO_CREATED event.
    """

    def _run(self, db_engine):
        po_success = _make_po_extraction_success()
        contract_success = _make_contract_extraction_success()
        invoice = _make_invoice_create()
        inv_success = _make_invoice_extraction_success(invoice)
        with (
            _patch_db(db_engine),
            patch(
                "ui.components.pipeline_runner.PurchaseOrderExtractionAgent.extract",
                return_value=po_success,
            ),
            patch(
                "ui.components.pipeline_runner.ContractExtractionAgent.extract",
                return_value=contract_success,
            ),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=inv_success,
            ),
        ):
            return run_extraction_pipeline_with_documents(
                invoice_text="(invoice text — patched)",
                po_text="(po text — patched)",
                contract_text="(contract text — patched)",
            )

    def test_only_one_vendor_row_created(
        self, db_session: Session, db_engine
    ) -> None:
        self._run(db_engine)
        SL = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
        with SL() as s:
            count = (
                s.query(VendorORM)
                .filter(VendorORM.vendor_code == _VENDOR_CODE)
                .count()
            )
        assert count == 1, f"Expected exactly 1 vendor row, found {count}"

    def test_contract_row_created(self, db_session: Session, db_engine) -> None:
        self._run(db_engine)
        SL = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
        with SL() as s:
            row = (
                s.query(ContractORM)
                .filter(ContractORM.contract_reference == _CONTRACT_REF)
                .first()
            )
        assert row is not None, "Contract row should have been persisted"

    def test_contract_uses_same_vendor_id_as_po(
        self, db_session: Session, db_engine
    ) -> None:
        self._run(db_engine)
        SL = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
        with SL() as s:
            vendor_row = (
                s.query(VendorORM)
                .filter(VendorORM.vendor_code == _VENDOR_CODE)
                .first()
            )
            po_row = (
                s.query(PurchaseOrderORM)
                .filter(PurchaseOrderORM.po_number == _PO_NUMBER)
                .first()
            )
            contract_row = (
                s.query(ContractORM)
                .filter(ContractORM.contract_reference == _CONTRACT_REF)
                .first()
            )
        assert vendor_row is not None
        assert po_row is not None
        assert contract_row is not None
        assert po_row.vendor_id == vendor_row.id
        assert contract_row.vendor_id == vendor_row.id

    def test_only_one_vendor_auto_created_event(
        self, db_session: Session, db_engine
    ) -> None:
        """Only one VENDOR_AUTO_CREATED event should exist — from the PO branch.
        The Contract branch reuses the existing vendor, so no second event."""
        self._run(db_engine)
        events = _vendor_audit_events()
        assert len(events) == 1, (
            f"Expected 1 VENDOR_AUTO_CREATED event, got {len(events)}: "
            f"{[json.loads(e['payload_json'])['source_document'] for e in events]}"
        )
        payload = json.loads(events[0]["payload_json"])
        assert payload["source_document"] == "PO"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 3 — Vendor auto-create followed by PO-level conflict
#              (commit-ordering proof)
# ─────────────────────────────────────────────────────────────────────────────


class TestVendorAutoCreateWithPOConflict:
    """
    Vendor does not exist initially. PO extraction succeeds, but upsert_po()
    returns UpsertConflict (po_number already exists with different data).
    session.commit() is NOT called → vendor row is NOT persisted →
    NO VENDOR_AUTO_CREATED audit event.

    This proves the commit-ordering constraint: the vendor INSERT and the PO
    INSERT share the same transaction; if the PO conflicts, neither is committed.
    """

    def _run(self, db_session: Session, db_engine):
        # Seed a DIFFERENT vendor so we have a valid FK for the conflict seed.
        seeded_vendor = VendorORM(
            vendor_code="SEED-VENDOR",
            name="Seed Vendor",
            is_active=True,
        )
        db_session.add(seeded_vendor)
        db_session.commit()

        # Seed the conflicting PO using the seeded vendor's id.
        _seed_po_conflict(db_session, seeded_vendor.id)

        po_success = _make_po_extraction_success(vendor_code=_VENDOR_CODE)
        invoice = _make_invoice_create()
        inv_success = _make_invoice_extraction_success(invoice)
        with (
            _patch_db(db_engine),
            patch(
                "ui.components.pipeline_runner.PurchaseOrderExtractionAgent.extract",
                return_value=po_success,
            ),
            patch(
                "ui.components.pipeline_runner.ContractExtractionAgent.extract",
                return_value=_make_contract_extraction_success(),
            ),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=inv_success,
            ),
        ):
            return run_extraction_pipeline_with_documents(
                invoice_text="(invoice text — patched)",
                po_text="(po text — patched)",
                contract_text=None,
            )

    def test_vendor_not_persisted_on_po_conflict(
        self, db_session: Session, db_engine
    ) -> None:
        """The auto-created vendor must NOT appear in the DB when the PO conflicts."""
        self._run(db_session, db_engine)
        SL = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
        with SL() as s:
            row = (
                s.query(VendorORM)
                .filter(VendorORM.vendor_code == _VENDOR_CODE)
                .first()
            )
        assert row is None, (
            "Vendor must NOT be persisted when the PO upsert conflicts "
            "(transaction must have been rolled back)"
        )

    def test_no_vendor_auto_created_audit_event_on_po_conflict(
        self, db_session: Session, db_engine
    ) -> None:
        """No VENDOR_AUTO_CREATED event should exist when the PO conflicts."""
        self._run(db_session, db_engine)
        events = _vendor_audit_events()
        assert len(events) == 0, (
            f"Expected 0 VENDOR_AUTO_CREATED events when PO conflicts, "
            f"got {len(events)}"
        )

    def test_result_is_exception_document_conflict(
        self, db_session: Session, db_engine
    ) -> None:
        """The pipeline result must be EXCEPTION / DOCUMENT_CONFLICT."""
        from models.enums import ExceptionReasonCode
        result = self._run(db_session, db_engine)
        assert result.outcome == "EXCEPTION"
        assert ExceptionReasonCode.DOCUMENT_CONFLICT.value in result.exception_reasons



# ─────────────────────────────────────────────────────────────────────────────
# Scenario 4 — End-to-end upload chain
# ─────────────────────────────────────────────────────────────────────────────


class TestEndToEndUploadChain:
    """
    PO uploaded (vendor auto-created) + contract uploaded (vendor reused) +
    invoice referencing both → invoice resolves vendor, PO, and contract
    successfully with no UNKNOWN_VENDOR / PO_NOT_FOUND / CONTRACT_NOT_FOUND
    reason codes.

    The pipeline result should be STP (all checks pass) because the PO total,
    contract unit price, and invoice amounts are all aligned.
    """

    def test_invoice_resolves_all_entities_no_vendor_exception(
        self, db_session: Session, db_engine
    ) -> None:
        po_success = _make_po_extraction_success()
        contract_success = _make_contract_extraction_success()
        invoice = _make_invoice_create()
        inv_success = _make_invoice_extraction_success(invoice)

        with (
            _patch_db(db_engine),
            patch(
                "ui.components.pipeline_runner.PurchaseOrderExtractionAgent.extract",
                return_value=po_success,
            ),
            patch(
                "ui.components.pipeline_runner.ContractExtractionAgent.extract",
                return_value=contract_success,
            ),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=inv_success,
            ),
        ):
            result = run_extraction_pipeline_with_documents(
                invoice_text="(invoice text — patched)",
                po_text="(po text — patched)",
                contract_text="(contract text — patched)",
            )

        from models.enums import ExceptionReasonCode
        blocked_codes = {
            ExceptionReasonCode.UNKNOWN_VENDOR.value,
            ExceptionReasonCode.PO_NOT_FOUND.value,
            ExceptionReasonCode.CONTRACT_NOT_FOUND.value,
        }
        found_blocked = blocked_codes & set(result.exception_reasons or [])
        assert not found_blocked, (
            f"Invoice should resolve all entities but got blocking reason codes: "
            f"{found_blocked}. Full reasons: {result.exception_reasons}"
        )

    def test_outcome_is_stp(self, db_session: Session, db_engine) -> None:
        """With matching PO/contract/invoice data, pipeline must return STP."""
        po_success = _make_po_extraction_success()
        contract_success = _make_contract_extraction_success()
        invoice = _make_invoice_create()
        inv_success = _make_invoice_extraction_success(invoice)

        with (
            _patch_db(db_engine),
            patch(
                "ui.components.pipeline_runner.PurchaseOrderExtractionAgent.extract",
                return_value=po_success,
            ),
            patch(
                "ui.components.pipeline_runner.ContractExtractionAgent.extract",
                return_value=contract_success,
            ),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=inv_success,
            ),
        ):
            result = run_extraction_pipeline_with_documents(
                invoice_text="(invoice text — patched)",
                po_text="(po text — patched)",
                contract_text="(contract text — patched)",
            )

        assert result.outcome == "STP", (
            f"Expected STP after auto-creating vendor + uploading PO + contract, "
            f"got {result.outcome!r}. "
            f"Error: {result.error_message!r}. "
            f"Reasons: {result.exception_reasons}"
        )

    def test_one_vendor_auto_created_event_in_audit(
        self, db_session: Session, db_engine
    ) -> None:
        """Exactly one VENDOR_AUTO_CREATED event should exist (PO branch only)."""
        po_success = _make_po_extraction_success()
        contract_success = _make_contract_extraction_success()
        invoice = _make_invoice_create()
        inv_success = _make_invoice_extraction_success(invoice)

        with (
            _patch_db(db_engine),
            patch(
                "ui.components.pipeline_runner.PurchaseOrderExtractionAgent.extract",
                return_value=po_success,
            ),
            patch(
                "ui.components.pipeline_runner.ContractExtractionAgent.extract",
                return_value=contract_success,
            ),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=inv_success,
            ),
        ):
            run_extraction_pipeline_with_documents(
                invoice_text="(invoice text — patched)",
                po_text="(po text — patched)",
                contract_text="(contract text — patched)",
            )

        events = _vendor_audit_events()
        assert len(events) == 1, (
            f"Expected exactly 1 VENDOR_AUTO_CREATED event, got {len(events)}"
        )
