"""
tests/integration/test_routing_integration.py — Phase 4 integration tests.

spec.md Phase 4 testing requirement:
  "Integration test that an exception invoice never appears in PaymentSchedule."

These tests wire Phase 3 (matching engine) and Phase 4 (routing) end-to-end
using real Pydantic objects — no mocks, no DB, no LLM.  They prove that the
full pipeline (MatchingEngine → route()) is structurally safe: an invoice that
fails any FR-3.1 check can never produce a PaymentSchedule.

Scenarios validated end-to-end (requirements.md §8):
  Scenario 1 — clean invoice       → STP, PaymentSchedule present
  Scenario 2 — price variance      → EXCEPTION, no PaymentSchedule
  Scenario 3 — missing approval    → EXCEPTION, no PaymentSchedule
  Scenario 6 — unknown vendor      → EXCEPTION, no PaymentSchedule
  Scenario 7 — PO not found        → EXCEPTION, no PaymentSchedule
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from matching.engine import MatchInput, MatchingEngine
from models.contract import ContractCreate, ContractLineItemCreate
from models.enums import ExceptionReasonCode, ExceptionStatus
from models.invoice import InvoiceCreate, InvoiceLineItemCreate
from models.purchase_order import POLineItemCreate, PurchaseOrderCreate
from models.vendor import VendorCreate
from routing.decision import ExceptionDecision, STPDecision, route

_ENGINE = MatchingEngine()

# ---------------------------------------------------------------------------
# Shared fixture builders (same data used by test_matching.py)
# ---------------------------------------------------------------------------


def _vendor(*, is_active: bool = True) -> VendorCreate:
    return VendorCreate(vendor_code="ACME-001", name="Acme Supplies Ltd", is_active=is_active)


def _po(
    *,
    po_total: str = "420.00",
    approval_threshold: str = "10000.00",
    line1_price: str = "38.00",
    line1_qty: str = "10",
) -> PurchaseOrderCreate:
    return PurchaseOrderCreate(
        po_number="PO-2025-0100",
        vendor_id="vendor-uuid-001",
        po_total=Decimal(po_total),
        approval_threshold=Decimal(approval_threshold),
        line_items=[
            POLineItemCreate(line_number=1, description="Widget Type A", qty=Decimal(line1_qty), unit_price=Decimal(line1_price)),
            POLineItemCreate(line_number=2, description="Shipping & Handling", qty=Decimal("1"), unit_price=Decimal("20.00")),
        ],
    )


def _contract(*, line1_price: str = "38.00") -> ContractCreate:
    return ContractCreate(
        contract_reference="CTR-2025-0018",
        vendor_id="vendor-uuid-001",
        line_items=[
            ContractLineItemCreate(line_number=1, description="Widget Type A", unit_price=Decimal(line1_price)),
            ContractLineItemCreate(line_number=2, description="Shipping & Handling", unit_price=Decimal("20.00")),
        ],
    )


def _invoice(
    *,
    grand_total: str = "420.00",
    subtotal: str = "400.00",
    tax: str = "20.00",
    line1_price: str = "38.00",
    line1_qty: str = "10",
    line1_amount: str = "380.00",
    invoice_number: str = "INV-2026-0042",
) -> InvoiceCreate:
    return InvoiceCreate(
        invoice_number=invoice_number,
        vendor_name="Acme Supplies Ltd",
        invoice_date="2026-01-15",
        po_reference="PO-2025-0100",
        contract_reference="CTR-2025-0018",
        subtotal=Decimal(subtotal),
        tax=Decimal(tax),
        grand_total=Decimal(grand_total),
        due_date="2026-02-14",
        payment_terms="Net 30",
        line_items=[
            InvoiceLineItemCreate(line_number=1, description="Widget Type A", qty=Decimal(line1_qty), unit_price=Decimal(line1_price), amount=Decimal(line1_amount)),
            InvoiceLineItemCreate(line_number=2, description="Shipping & Handling", qty=Decimal("1"), unit_price=Decimal("20.00"), amount=Decimal("20.00")),
        ],
    )


def _run(
    inv: InvoiceCreate,
    po: PurchaseOrderCreate | None,
    contract: ContractCreate | None,
    vendor: VendorCreate | None,
    approval_on_file: bool = False,
    tolerance_pct: Decimal = Decimal("0"),
):
    """Run MatchingEngine then route() and return the RoutingDecision."""
    match_input = MatchInput(
        invoice=inv,
        purchase_order=po,
        contract=contract,
        vendor=vendor,
        approval_on_file=approval_on_file,
        tolerance_pct=tolerance_pct,
        invoice_id=inv.invoice_number,
    )
    match_result = _ENGINE.run(match_input)
    return route(match_result, inv)


# ---------------------------------------------------------------------------
# Core safety property: exception invoices are STRUCTURALLY EXCLUDED from
# PaymentSchedule — not just logically excluded by a flag check.
# ---------------------------------------------------------------------------


class TestExceptionNeverInPaymentSchedule:
    """
    FR-4.1 enforcement: ExceptionDecision carries no payment_schedule.
    This is a structural guarantee in the type system, not just a runtime check.
    """

    def test_unknown_vendor_has_no_payment_schedule(self):
        result = _run(_invoice(), _po(), _contract(), vendor=None)
        assert isinstance(result, ExceptionDecision)
        assert not hasattr(result, "payment_schedule"), \
            "ExceptionDecision must not have a payment_schedule attribute."

    def test_po_not_found_has_no_payment_schedule(self):
        result = _run(_invoice(), po=None, contract=_contract(), vendor=_vendor())
        assert isinstance(result, ExceptionDecision)
        assert not hasattr(result, "payment_schedule")

    def test_contract_not_found_has_no_payment_schedule(self):
        result = _run(_invoice(), po=_po(), contract=None, vendor=_vendor())
        assert isinstance(result, ExceptionDecision)
        assert not hasattr(result, "payment_schedule")

    def test_price_variance_has_no_payment_schedule(self):
        # $42 billed vs $38 contract → PRICE_VARIANCE
        result = _run(
            _invoice(line1_price="42.00", line1_amount="420.00", subtotal="440.00", grand_total="440.00"),
            _po(po_total="440.00"),
            _contract(line1_price="38.00"),
            vendor=_vendor(),
        )
        assert isinstance(result, ExceptionDecision)
        assert not hasattr(result, "payment_schedule")

    def test_missing_approval_has_no_payment_schedule(self):
        # $12,000 over $10,000 threshold, no approval
        result = _run(
            _invoice(grand_total="12000.00", subtotal="11000.00", tax="1000.00",
                     line1_price="1100.00", line1_qty="10", line1_amount="11000.00"),
            _po(po_total="12000.00", approval_threshold="10000.00",
                line1_price="1100.00", line1_qty="10"),
            _contract(line1_price="1100.00"),
            vendor=_vendor(),
            approval_on_file=False,
        )
        assert isinstance(result, ExceptionDecision)
        assert not hasattr(result, "payment_schedule")

    def test_only_stp_decision_has_payment_schedule(self):
        """Positive test: STPDecision has the attribute; ExceptionDecision does not."""
        stp = _run(_invoice(), _po(), _contract(), vendor=_vendor())
        exc = _run(_invoice(), po=None, contract=_contract(), vendor=_vendor())

        assert isinstance(stp, STPDecision)
        assert hasattr(stp, "payment_schedule")

        assert isinstance(exc, ExceptionDecision)
        assert not hasattr(exc, "payment_schedule")


# ---------------------------------------------------------------------------
# Scenario 1 — clean invoice end-to-end → STP
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestScenario01EndToEnd:
    """
    requirements.md §8 scenario 1: fully matches PO and contract.
    Pipeline: MatchingEngine → route() → STPDecision + PaymentSchedule.
    """

    def test_routes_stp(self):
        result = _run(_invoice(), _po(), _contract(), vendor=_vendor())
        assert isinstance(result, STPDecision)

    def test_payment_schedule_amount(self):
        result = _run(_invoice(), _po(), _contract(), vendor=_vendor())
        assert isinstance(result, STPDecision)
        assert result.payment_schedule.amount == Decimal("420.00")

    def test_payment_schedule_invoice_id(self):
        result = _run(_invoice(invoice_number="INV-S1"), _po(), _contract(), vendor=_vendor())
        assert isinstance(result, STPDecision)
        assert result.payment_schedule.invoice_id == "INV-S1"


# ---------------------------------------------------------------------------
# Scenario 2 — price variance → EXCEPTION
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestScenario02EndToEnd:
    """
    requirements.md §8 scenario 2: $42/unit billed vs $38 contract.
    Pipeline: PRICE_VARIANCE → ExceptionDecision, no PaymentSchedule.
    """

    def test_routes_exception(self):
        result = _run(
            _invoice(line1_price="42.00", line1_amount="420.00", subtotal="440.00", grand_total="440.00"),
            _po(po_total="440.00"),
            _contract(line1_price="38.00"),
            vendor=_vendor(),
        )
        assert isinstance(result, ExceptionDecision)

    def test_reason_code_is_price_variance(self):
        result = _run(
            _invoice(line1_price="42.00", line1_amount="420.00", subtotal="440.00", grand_total="440.00"),
            _po(po_total="440.00"),
            _contract(line1_price="38.00"),
            vendor=_vendor(),
        )
        assert isinstance(result, ExceptionDecision)
        codes = {r.reason_code for r in result.exception_record.reasons}
        assert ExceptionReasonCode.PRICE_VARIANCE in codes

    def test_not_scheduled(self):
        result = _run(
            _invoice(line1_price="42.00", line1_amount="420.00", subtotal="440.00", grand_total="440.00"),
            _po(po_total="440.00"),
            _contract(line1_price="38.00"),
            vendor=_vendor(),
        )
        assert not hasattr(result, "payment_schedule")


# ---------------------------------------------------------------------------
# Scenario 3 — missing approval → EXCEPTION
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestScenario03EndToEnd:
    """
    requirements.md §8 scenario 3: over $10k, no approval.
    Pipeline: MISSING_APPROVAL → ExceptionDecision.
    """

    def test_routes_exception(self):
        result = _run(
            _invoice(grand_total="12000.00", subtotal="11000.00", tax="1000.00",
                     line1_price="1100.00", line1_qty="10", line1_amount="11000.00"),
            _po(po_total="12000.00", approval_threshold="10000.00",
                line1_price="1100.00", line1_qty="10"),
            _contract(line1_price="1100.00"),
            vendor=_vendor(),
            approval_on_file=False,
        )
        assert isinstance(result, ExceptionDecision)

    def test_with_approval_routes_stp(self):
        result = _run(
            _invoice(grand_total="12000.00", subtotal="11000.00", tax="1000.00",
                     line1_price="1100.00", line1_qty="10", line1_amount="11000.00"),
            _po(po_total="12000.00", approval_threshold="10000.00",
                line1_price="1100.00", line1_qty="10"),
            _contract(line1_price="1100.00"),
            vendor=_vendor(),
            approval_on_file=True,
        )
        assert isinstance(result, STPDecision)

    def test_reason_code_is_missing_approval(self):
        result = _run(
            _invoice(grand_total="12000.00", subtotal="11000.00", tax="1000.00",
                     line1_price="1100.00", line1_qty="10", line1_amount="11000.00"),
            _po(po_total="12000.00", approval_threshold="10000.00",
                line1_price="1100.00", line1_qty="10"),
            _contract(line1_price="1100.00"),
            vendor=_vendor(),
            approval_on_file=False,
        )
        assert isinstance(result, ExceptionDecision)
        codes = {r.reason_code for r in result.exception_record.reasons}
        assert ExceptionReasonCode.MISSING_APPROVAL in codes


# ---------------------------------------------------------------------------
# Scenario 6 — unknown vendor → EXCEPTION
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestScenario06EndToEnd:
    """
    requirements.md §8 scenario 6: vendor not in vendor master.
    Pipeline: UNKNOWN_VENDOR → ExceptionDecision, regardless of price/qty match.
    """

    def test_routes_exception(self):
        result = _run(_invoice(), _po(), _contract(), vendor=None)
        assert isinstance(result, ExceptionDecision)

    def test_reason_code_is_unknown_vendor(self):
        result = _run(_invoice(), _po(), _contract(), vendor=None)
        assert isinstance(result, ExceptionDecision)
        codes = {r.reason_code for r in result.exception_record.reasons}
        assert ExceptionReasonCode.UNKNOWN_VENDOR in codes

    def test_inactive_vendor_also_routes_exception(self):
        result = _run(_invoice(), _po(), _contract(), vendor=_vendor(is_active=False))
        assert isinstance(result, ExceptionDecision)


# ---------------------------------------------------------------------------
# Scenario 7 — PO not found → EXCEPTION
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestScenario07EndToEnd:
    """
    requirements.md §8 scenario 7: PO reference doesn't resolve.
    Pipeline: PO_NOT_FOUND → ExceptionDecision. No silent failure.
    """

    def test_routes_exception(self):
        result = _run(_invoice(), po=None, contract=_contract(), vendor=_vendor())
        assert isinstance(result, ExceptionDecision)

    def test_reason_code_is_po_not_found(self):
        result = _run(_invoice(), po=None, contract=_contract(), vendor=_vendor())
        assert isinstance(result, ExceptionDecision)
        codes = {r.reason_code for r in result.exception_record.reasons}
        assert ExceptionReasonCode.PO_NOT_FOUND in codes

    def test_no_silent_failure(self):
        """Outcome is a typed ExceptionDecision — never None or a partial STP."""
        result = _run(_invoice(), po=None, contract=_contract(), vendor=_vendor())
        assert result is not None
        assert isinstance(result, ExceptionDecision)
        assert result.exception_record.status == ExceptionStatus.OPEN

    def test_not_scheduled(self):
        result = _run(_invoice(), po=None, contract=_contract(), vendor=_vendor())
        assert not hasattr(result, "payment_schedule")
