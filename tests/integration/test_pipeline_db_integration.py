"""
tests/integration/test_pipeline_db_integration.py
─────────────────────────────────────────────────
Integration tests that wire the full pipeline path:

  DB seed (vendor / PO / contract)
    → run_extraction_pipeline()          ← LLM layer is patched out
      → DB entity resolution             ← real db/resolver.py + repositories
        → MatchingEngine                 ← real matching engine
          → route()                      ← real routing / decision
            → run_pipeline()             ← real service layer

The LLM extraction agent is patched to return a fixed ExtractionSuccess so
these tests run without a network connection and without OPENROUTER_API_KEY.
Everything else — the database session, repository lookups, matching engine,
routing, and audit writer — runs against real code.

Scenarios
─────────
Case 1 — STP path
  Vendor INTEG-V1 / PO-INTEG-01 / CTR-INTEG-01 are seeded.
  The patched extractor returns an InvoiceCreate whose line items and totals
  match the PO and contract exactly.  Expected outcome: "STP".

Case 2 — PRICE_VARIANCE exception path
  Same vendor + PO seeded, but the contract is seeded with unit_price=100.00
  while the invoice bills at 115.00 (15% over).  At the default 0% tolerance
  the prices_match check fails.  Expected outcome: "EXCEPTION" with reason
  code PRICE_VARIANCE.

Isolation strategy
──────────────────
Each test gets a dedicated SQLite in-memory database via the `db_engine`
fixture.  db/session.py's module-level `engine` and `SessionLocal` are
patched to point at the test engine so that run_extraction_pipeline()'s
call to get_session() uses the seeded test DB, not app.db.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from models.base import Base
from models.vendor import VendorORM
from models.purchase_order import PurchaseOrderORM, POLineItemORM
from models.contract import ContractORM, ContractLineItemORM
from models.invoice import InvoiceCreate, InvoiceLineItemCreate
from models.enums import ExtractionStatus, InvoiceStatus
from extraction.schemas import ExtractionSuccess
from ui.components.pipeline_runner import run_extraction_pipeline


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

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
    """Session bound to the in-memory engine; rolls back after each test."""
    SessionLocal = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    yield session
    session.close()


def _patch_db_session(db_engine):
    """
    Return a context-manager patcher that redirects db.session.get_session()
    to use db_engine instead of the production engine.

    db/session.py's module-level `SessionLocal` is replaced with a factory
    bound to the test engine.  `get_session()` is a @contextmanager that
    creates sessions from `SessionLocal`, so patching `SessionLocal` is
    sufficient without reimplementing the context manager logic.
    """
    TestSessionLocal = sessionmaker(
        bind=db_engine, autocommit=False, autoflush=False
    )
    return patch("db.session.SessionLocal", TestSessionLocal)


# ──────────────────────────────────────────────────────────────────────────────
# Seed helpers
# ──────────────────────────────────────────────────────────────────────────────

def _seed_vendor(session: Session) -> VendorORM:
    vendor = VendorORM(
        vendor_code="INTEG-V1",
        name="Integration Vendor One",
        contact_email="ap@integ.example.com",
        is_active=True,
    )
    session.add(vendor)
    session.flush()
    return vendor


def _seed_po(
    session: Session,
    vendor_id: str,
    *,
    unit_price: str = "250.00",
    qty: str = "10",
    po_total: str = "2500.00",
) -> PurchaseOrderORM:
    po = PurchaseOrderORM(
        po_number="PO-INTEG-01",
        vendor_id=vendor_id,
        po_total=po_total,
        approval_threshold="10000.00",
        notes="Integration test PO",
        line_items=[
            POLineItemORM(
                line_number=1,
                description="Test Widget",
                qty=qty,
                unit_price=unit_price,
            )
        ],
    )
    session.add(po)
    session.flush()
    return po


def _seed_contract(
    session: Session,
    vendor_id: str,
    *,
    unit_price: str = "250.00",
) -> ContractORM:
    contract = ContractORM(
        contract_reference="CTR-INTEG-01",
        vendor_id=vendor_id,
        discount_term_raw="2/10 net 30",
        discount_pct="0.02",
        discount_days=10,
        net_days=30,
        approval_threshold=None,
        notes="Integration test contract",
        line_items=[
            ContractLineItemORM(
                line_number=1,
                description="Test Widget",
                unit_price=unit_price,
            )
        ],
    )
    session.add(contract)
    session.flush()
    return contract


def _make_invoice_create(
    *,
    unit_price: str = "250.00",
    qty: str = "10",
    grand_total: str = "2500.00",
    subtotal: str = "2500.00",
) -> InvoiceCreate:
    """Build an InvoiceCreate whose references match the seeded PO/contract."""
    invoice_date = date.today() - timedelta(days=5)
    due_date = invoice_date + timedelta(days=30)
    line_amount = (Decimal(qty) * Decimal(unit_price)).quantize(Decimal("0.01"))
    return InvoiceCreate(
        invoice_number="INV-INTEG-001",
        vendor_name="Integration Vendor One",
        invoice_date=invoice_date,
        po_reference="PO-INTEG-01",
        contract_reference="CTR-INTEG-01",
        payment_terms="Net 30",
        subtotal=Decimal(subtotal),
        tax=Decimal("0.00"),
        grand_total=Decimal(grand_total),
        due_date=due_date,
        line_items=[
            InvoiceLineItemCreate(
                line_number=1,
                description="Test Widget",
                qty=Decimal(qty),
                unit_price=Decimal(unit_price),
                amount=line_amount,
            )
        ],
        extraction_status=ExtractionStatus.EXTRACTED,
        invoice_status=InvoiceStatus.EXTRACTED,
    )


def _make_extraction_success(invoice: InvoiceCreate) -> ExtractionSuccess:
    """Wrap an InvoiceCreate in an ExtractionSuccess as the LLM mock returns."""
    return ExtractionSuccess(
        invoice=invoice,
        raw_payload='{"mocked": true}',
        attempt_count=1,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Case 1 — STP path
# ──────────────────────────────────────────────────────────────────────────────

class TestSTPPath:
    """
    Seeded vendor / PO / contract all match the invoice exactly.
    run_extraction_pipeline() must return outcome='STP'.
    """

    def test_outcome_is_stp(self, db_session: Session, db_engine) -> None:
        # Seed
        vendor = _seed_vendor(db_session)
        _seed_po(db_session, vendor.id)
        _seed_contract(db_session, vendor.id)
        db_session.commit()

        invoice = _make_invoice_create()
        extraction_success = _make_extraction_success(invoice)

        with (
            _patch_db_session(db_engine),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=extraction_success,
            ),
        ):
            result = run_extraction_pipeline("(invoice text — LLM is patched)")

        assert result.outcome == "STP", (
            f"Expected STP but got {result.outcome!r}. "
            f"Error: {result.error_message!r}. "
            f"Exception reasons: {result.exception_reasons}"
        )

    def test_stp_has_payment_schedule(self, db_session: Session, db_engine) -> None:
        vendor = _seed_vendor(db_session)
        _seed_po(db_session, vendor.id)
        _seed_contract(db_session, vendor.id)
        db_session.commit()

        invoice = _make_invoice_create()
        with (
            _patch_db_session(db_engine),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=_make_extraction_success(invoice),
            ),
        ):
            result = run_extraction_pipeline("(invoice text — LLM is patched)")

        assert result.payment_schedule is not None
        assert result.payment_schedule["amount"] == "2500.00"

    def test_stp_has_no_exception_reasons(self, db_session: Session, db_engine) -> None:
        vendor = _seed_vendor(db_session)
        _seed_po(db_session, vendor.id)
        _seed_contract(db_session, vendor.id)
        db_session.commit()

        invoice = _make_invoice_create()
        with (
            _patch_db_session(db_engine),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=_make_extraction_success(invoice),
            ),
        ):
            result = run_extraction_pipeline("(invoice text — LLM is patched)")

        assert not result.exception_reasons


# ──────────────────────────────────────────────────────────────────────────────
# Case 2 — PRICE_VARIANCE exception path
# ──────────────────────────────────────────────────────────────────────────────

class TestPriceVarianceExceptionPath:
    """
    Contract unit_price is seeded at 100.00.
    Invoice bills at 115.00 — 15% over contract, which exceeds the 0% default
    tolerance.  prices_match fails → route() returns ExceptionDecision with
    PRICE_VARIANCE reason code.
    """

    def test_outcome_is_exception(self, db_session: Session, db_engine) -> None:
        vendor = _seed_vendor(db_session)
        # PO: qty=10, unit_price=100.00, po_total=1000.00
        _seed_po(
            db_session, vendor.id,
            unit_price="100.00", qty="10", po_total="1150.00",
        )
        # Contract: contracted price=100.00 — invoice will bill 115.00
        _seed_contract(db_session, vendor.id, unit_price="100.00")
        db_session.commit()

        # Invoice bills 115.00 per unit — 15% over contract
        invoice = _make_invoice_create(
            unit_price="115.00",
            qty="10",
            grand_total="1150.00",
            subtotal="1150.00",
        )
        with (
            _patch_db_session(db_engine),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=_make_extraction_success(invoice),
            ),
        ):
            result = run_extraction_pipeline("(invoice text — LLM is patched)")

        assert result.outcome == "EXCEPTION", (
            f"Expected EXCEPTION but got {result.outcome!r}. "
            f"Error: {result.error_message!r}"
        )

    def test_exception_reason_is_price_variance(
        self, db_session: Session, db_engine
    ) -> None:
        vendor = _seed_vendor(db_session)
        _seed_po(
            db_session, vendor.id,
            unit_price="100.00", qty="10", po_total="1150.00",
        )
        _seed_contract(db_session, vendor.id, unit_price="100.00")
        db_session.commit()

        invoice = _make_invoice_create(
            unit_price="115.00",
            qty="10",
            grand_total="1150.00",
            subtotal="1150.00",
        )
        with (
            _patch_db_session(db_engine),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=_make_extraction_success(invoice),
            ),
        ):
            result = run_extraction_pipeline("(invoice text — LLM is patched)")

        from models.enums import ExceptionReasonCode
        assert ExceptionReasonCode.PRICE_VARIANCE.value in result.exception_reasons, (
            f"PRICE_VARIANCE not in reasons: {result.exception_reasons}"
        )

    def test_exception_has_no_payment_schedule(
        self, db_session: Session, db_engine
    ) -> None:
        """An exception invoice must never produce a payment schedule (FR-4.1)."""
        vendor = _seed_vendor(db_session)
        _seed_po(
            db_session, vendor.id,
            unit_price="100.00", qty="10", po_total="1150.00",
        )
        _seed_contract(db_session, vendor.id, unit_price="100.00")
        db_session.commit()

        invoice = _make_invoice_create(
            unit_price="115.00",
            qty="10",
            grand_total="1150.00",
            subtotal="1150.00",
        )
        with (
            _patch_db_session(db_engine),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=_make_extraction_success(invoice),
            ),
        ):
            result = run_extraction_pipeline("(invoice text — LLM is patched)")

        assert result.payment_schedule is None

    def test_exception_only_price_variance_not_total_mismatch(
        self, db_session: Session, db_engine
    ) -> None:
        """
        The PO total is set to match the billed grand_total (1150.00) so
        total_matches passes.  Only prices_match fails, giving exactly one
        reason code: PRICE_VARIANCE.
        """
        vendor = _seed_vendor(db_session)
        _seed_po(
            db_session, vendor.id,
            unit_price="100.00", qty="10", po_total="1150.00",
        )
        _seed_contract(db_session, vendor.id, unit_price="100.00")
        db_session.commit()

        invoice = _make_invoice_create(
            unit_price="115.00",
            qty="10",
            grand_total="1150.00",
            subtotal="1150.00",
        )
        with (
            _patch_db_session(db_engine),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent.extract",
                return_value=_make_extraction_success(invoice),
            ),
        ):
            result = run_extraction_pipeline("(invoice text — LLM is patched)")

        from models.enums import ExceptionReasonCode
        assert result.exception_reasons == [ExceptionReasonCode.PRICE_VARIANCE.value], (
            f"Expected exactly [PRICE_VARIANCE], got {result.exception_reasons}"
        )
