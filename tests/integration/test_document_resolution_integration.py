"""
tests/integration/test_document_resolution_integration.py
──────────────────────────────────────────────────────────
Integration tests for the document-upload resolution order in
run_extraction_pipeline_with_documents().

Spec under test (ui/components/pipeline_runner.py, step 6 comment):
  a. If a PO/contract document was uploaded this run AND its upsert succeeded
     → use the record returned by upsert_po() / upsert_contract() directly
     (the freshly-extracted row, already persisted).
  b. If no document was uploaded, or extraction failed (non-blocking)
     → fall back to get_po_by_number() / get_contract_by_reference() using
     the reference strings embedded in the extracted invoice.
  c. If neither path resolves an entity, None is passed to run_pipeline()
     and the matching engine raises PO_NOT_FOUND / CONTRACT_NOT_FOUND.

All LLM calls are patched — no network required.
Every test gets a fresh in-memory SQLite database.
The production db.session.SessionLocal is redirected to the test engine so
that run_extraction_pipeline_with_documents()'s get_session() calls hit the
test DB, not app.db.

Test classes
────────────
TestUploadAndMatchPath          — PO + contract uploaded, upserted, used for STP.
TestReferenceOnlyMatchPath      — PO + contract seeded in DB, no upload; DB fallback → STP.
TestMixedPath                   — PO uploaded (new row), contract from DB only → STP.
TestNoDocumentsNoPreviousRecord — nothing uploaded, nothing in DB → PO_NOT_FOUND exception.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import audit.writer as audit_writer
from models.base import Base
from models.contract import ContractORM, ContractLineItemORM
from models.enums import ExceptionReasonCode, ExtractionStatus, InvoiceStatus
from models.invoice import InvoiceCreate, InvoiceLineItemCreate
from models.purchase_order import PurchaseOrderORM, POLineItemORM
from models.vendor import VendorORM
from extraction.contract_schemas import ContractExtractionSuccess
from extraction.po_schemas import POExtractionSuccess
from extraction.schemas import ExtractionSuccess
from models.contract import ContractCreate, ContractLineItemCreate, DiscountTermSchema
from models.purchase_order import PurchaseOrderCreate, POLineItemCreate
from ui.components.pipeline_runner import run_extraction_pipeline_with_documents


# ─────────────────────────────────────────────────────────────────────────────
# Shared constants
# ─────────────────────────────────────────────────────────────────────────────

_PO_NUMBER       = "PO-DOCRES-001"
_CONTRACT_REF    = "CTR-DOCRES-001"
_VENDOR_CODE     = "DOCRES-V1"
_UNIT_PRICE      = Decimal("200.00")
_QTY             = Decimal("5")
_LINE_AMOUNT     = _UNIT_PRICE * _QTY          # 1000.00
_GRAND_TOTAL     = _LINE_AMOUNT                # 1000.00
_INVOICE_DATE    = date.today() - timedelta(days=3)
_DUE_DATE        = _INVOICE_DATE + timedelta(days=30)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_engine():
    """Fresh in-memory SQLite engine per test."""
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
    TestSL = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    session = TestSL()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def clear_audit(db_engine):
    """Reset the in-process audit log before every test."""
    audit_writer.clear_audit_log()
    yield
    audit_writer.clear_audit_log()


def _patch_session(db_engine):
    """Redirect db.session.SessionLocal to the test engine."""
    TestSL = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    return patch("db.session.SessionLocal", TestSL)


# ─────────────────────────────────────────────────────────────────────────────
# Seed helpers
# ─────────────────────────────────────────────────────────────────────────────

def _seed_vendor(session: Session) -> VendorORM:
    v = VendorORM(
        vendor_code=_VENDOR_CODE,
        name="Doc-Resolution Vendor",
        contact_email="dr@example.com",
        is_active=True,
    )
    session.add(v)
    session.flush()
    return v


def _seed_po(session: Session, vendor_id: str, *, unit_price: str | None = None) -> PurchaseOrderORM:
    up = unit_price or str(_UNIT_PRICE)
    po = PurchaseOrderORM(
        po_number=_PO_NUMBER,
        vendor_id=vendor_id,
        po_total=str(_GRAND_TOTAL),
        approval_threshold="50000.00",
        notes=None,
        line_items=[
            POLineItemORM(
                line_number=1,
                description="Doc Widget",
                qty=str(_QTY),
                unit_price=up,
            )
        ],
    )
    session.add(po)
    session.flush()
    return po


def _seed_contract(session: Session, vendor_id: str, *, unit_price: str | None = None) -> ContractORM:
    up = unit_price or str(_UNIT_PRICE)
    c = ContractORM(
        contract_reference=_CONTRACT_REF,
        vendor_id=vendor_id,
        discount_term_raw="2/10 net 30",
        discount_pct="0.02",
        discount_days=10,
        net_days=30,
        approval_threshold=None,
        notes=None,
        line_items=[
            ContractLineItemORM(
                line_number=1,
                description="Doc Widget",
                unit_price=up,
            )
        ],
    )
    session.add(c)
    session.flush()
    return c


# ─────────────────────────────────────────────────────────────────────────────
# Mock-object builders
# ─────────────────────────────────────────────────────────────────────────────

def _make_invoice_create() -> InvoiceCreate:
    """InvoiceCreate whose po_reference/contract_reference match the seeded rows."""
    return InvoiceCreate(
        invoice_number="INV-DOCRES-001",
        vendor_name="Doc-Resolution Vendor",
        invoice_date=_INVOICE_DATE,
        po_reference=_PO_NUMBER,
        contract_reference=_CONTRACT_REF,
        payment_terms="Net 30",
        subtotal=_GRAND_TOTAL,
        tax=Decimal("0.00"),
        grand_total=_GRAND_TOTAL,
        due_date=_DUE_DATE,
        line_items=[
            InvoiceLineItemCreate(
                line_number=1,
                description="Doc Widget",
                qty=_QTY,
                unit_price=_UNIT_PRICE,
                amount=_LINE_AMOUNT,
            )
        ],
        extraction_status=ExtractionStatus.EXTRACTED,
        invoice_status=InvoiceStatus.EXTRACTED,
    )


def _invoice_extraction_success(invoice: InvoiceCreate) -> ExtractionSuccess:
    return ExtractionSuccess(
        invoice=invoice,
        raw_payload='{"mocked": true}',
        attempt_count=1,
    )


def _po_extraction_success(vendor_id: str = "DOCRES-V1") -> POExtractionSuccess:
    """POExtractionSuccess wrapping a PurchaseOrderCreate matching seeded values."""
    po = PurchaseOrderCreate(
        po_number=_PO_NUMBER,
        vendor_id=vendor_id,
        po_total=_GRAND_TOTAL,
        approval_threshold=Decimal("50000.00"),
        notes=None,
        line_items=[
            POLineItemCreate(
                line_number=1,
                description="Doc Widget",
                qty=_QTY,
                unit_price=_UNIT_PRICE,
            )
        ],
    )
    return POExtractionSuccess(
        po=po,
        vendor_code_extracted=vendor_id,
        raw_payload='{"mocked_po": true}',
        attempt_count=1,
    )


def _contract_extraction_success(vendor_id: str = "DOCRES-V1") -> ContractExtractionSuccess:
    """ContractExtractionSuccess wrapping a ContractCreate matching seeded values."""
    dt = DiscountTermSchema(
        discount_term_raw="2/10 net 30",
        discount_pct=Decimal("0.02"),
        discount_days=10,
        net_days=30,
    )
    contract = ContractCreate(
        contract_reference=_CONTRACT_REF,
        vendor_id=vendor_id,
        discount_term=dt,
        approval_threshold=None,
        notes=None,
        line_items=[
            ContractLineItemCreate(
                line_number=1,
                description="Doc Widget",
                unit_price=_UNIT_PRICE,
            )
        ],
    )
    return ContractExtractionSuccess(
        contract=contract,
        vendor_code_extracted=vendor_id,
        discount_term_raw="2/10 net 30",
        raw_payload='{"mocked_contract": true}',
        attempt_count=1,
    )
