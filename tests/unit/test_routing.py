"""
tests/unit/test_routing.py — Phase 4 unit tests for routing/decision.py.

spec.md Phase 4 testing requirement:
  "Unit tests asserting the FR-3.1 boolean logic exactly (every combination
   of passing/failing sub-conditions)."

All tests are pure — no DB, no network, no LLM.  Input objects are assembled
from Phase 3's MatchResultCreate and Phase 2's InvoiceCreate.

Test classes:
  TestSTPRouting            — all FR-3.1 checks pass → STPDecision
  TestExceptionRouting      — each individual sub-condition failing → EXCEPTION
  TestMultipleFailures      — several sub-conditions failing → one EXCEPTION
                              with multiple reason codes
  TestReasonCodeMapping     — correct ExceptionReasonCode for each condition
  TestSTPPaymentSchedule    — STPDecision carries correct PaymentSchedule
  TestExceptionRecord       — ExceptionDecision carries correct record shape
  TestHumanGateEndpoints    — approve / reject API endpoints (via TestClient)
  TestScenario01Full        — scenario 1 (clean) → STP
  TestScenario02Full        — scenario 2 (price variance) → EXCEPTION
  TestScenario03Full        — scenario 3 (missing approval) → EXCEPTION
  TestScenario06Full        — scenario 6 (unknown vendor) → EXCEPTION
  TestScenario07Full        — scenario 7 (PO not found) → EXCEPTION
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from api.exceptions import clear_store, register_exception
from models.enums import ExceptionReasonCode, ExceptionStatus, HumanAction
from models.invoice import InvoiceCreate, InvoiceLineItemCreate
from models.match_result import LineItemMatchDetail, MatchResultCreate
from models.payment_schedule import PaymentScheduleCreate
from routing.decision import (
    ExceptionDecision,
    STPDecision,
    route,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers — build test objects
# ---------------------------------------------------------------------------


def _match(
    *,
    vendor_known: bool = True,
    po_resolved: bool = True,
    contract_resolved: bool = True,
    quantities_match: bool = True,
    prices_match: bool = True,
    total_matches: bool = True,
    approval_satisfied: bool = True,
    invoice_id: str = "INV-TEST-001",
    total_variance_abs: str | None = None,
    total_variance_pct: str | None = None,
    line_details: list[LineItemMatchDetail] | None = None,
) -> MatchResultCreate:
    overall = (
        vendor_known
        and po_resolved
        and contract_resolved
        and quantities_match
        and prices_match
        and total_matches
        and approval_satisfied
    )
    return MatchResultCreate(
        invoice_id=invoice_id,
        vendor_known=vendor_known,
        po_resolved=po_resolved,
        contract_resolved=contract_resolved,
        quantities_match=quantities_match,
        prices_match=prices_match,
        total_matches=total_matches,
        approval_satisfied=approval_satisfied,
        overall_passed=overall,
        total_variance_abs=Decimal(total_variance_abs) if total_variance_abs else None,
        total_variance_pct=Decimal(total_variance_pct) if total_variance_pct else None,
        line_item_details=line_details or [],
    )


def _invoice(
    *,
    invoice_number: str = "INV-TEST-001",
    grand_total: str = "420.00",
    due_date: str = "2026-02-14",
) -> InvoiceCreate:
    return InvoiceCreate(
        invoice_number=invoice_number,
        vendor_name="Acme Supplies Ltd",
        invoice_date="2026-01-15",
        po_reference="PO-2025-0100",
        contract_reference="CTR-2025-0018",
        subtotal=Decimal("400.00"),
        tax=Decimal("20.00"),
        grand_total=Decimal(grand_total),
        due_date=due_date,
        payment_terms="Net 30",
        line_items=[
            InvoiceLineItemCreate(
                line_number=1,
                description="Widget Type A",
                qty=Decimal("10"),
                unit_price=Decimal("38.00"),
                amount=Decimal("380.00"),
            ),
            InvoiceLineItemCreate(
                line_number=2,
                description="Shipping & Handling",
                qty=Decimal("1"),
                unit_price=Decimal("20.00"),
                amount=Decimal("20.00"),
            ),
        ],
    )


def _line_detail(
    *,
    line_number: int = 1,
    billed_qty: str = "10",
    ordered_qty: str = "10",
    billed_price: str = "38.00",
    contract_price: str = "38.00",
    price_match: bool = True,
    qty_match: bool = True,
) -> LineItemMatchDetail:
    qty_var = Decimal(billed_qty) - Decimal(ordered_qty)
    price_var_abs = Decimal(billed_price) - Decimal(contract_price)
    price_var_pct = (
        price_var_abs / Decimal(contract_price)
        if Decimal(contract_price) != 0
        else Decimal("0")
    )
    return LineItemMatchDetail(
        line_number=line_number,
        billed_qty=Decimal(billed_qty),
        ordered_qty=Decimal(ordered_qty),
        qty_variance_abs=qty_var,
        qty_match=qty_match,
        billed_unit_price=Decimal(billed_price),
        contract_unit_price=Decimal(contract_price),
        price_variance_abs=price_var_abs,
        price_variance_pct=price_var_pct,
        price_match=price_match,
    )


# ---------------------------------------------------------------------------
# STP routing — all checks pass
# ---------------------------------------------------------------------------


class TestSTPRouting:
    def test_all_pass_returns_stp_decision(self):
        result = route(_match(), _invoice())
        assert isinstance(result, STPDecision)

    def test_stp_outcome_field(self):
        result = route(_match(), _invoice())
        assert result.outcome == "STP"

    def test_stp_invoice_id_set(self):
        result = route(_match(invoice_id="INV-001"), _invoice())
        assert result.invoice_id == "INV-001"

    def test_stp_carries_payment_schedule(self):
        result = route(_match(), _invoice())
        assert isinstance(result, STPDecision)
        assert isinstance(result.payment_schedule, PaymentScheduleCreate)

    def test_stp_payment_amount_equals_grand_total(self):
        result = route(_match(), _invoice(grand_total="420.00"))
        assert isinstance(result, STPDecision)
        assert result.payment_schedule.amount == Decimal("420.00")

    def test_stp_payment_date_equals_invoice_due_date(self):
        result = route(_match(), _invoice(due_date="2026-03-01"))
        assert isinstance(result, STPDecision)
        assert result.payment_schedule.scheduled_date == date(2026, 3, 1)

    def test_stp_payment_date_can_be_overridden(self):
        override = date(2026, 1, 25)
        result = route(_match(), _invoice(), payment_due_date=override)
        assert isinstance(result, STPDecision)
        assert result.payment_schedule.scheduled_date == override

    def test_stp_discount_taken_defaults_false(self):
        result = route(_match(), _invoice())
        assert isinstance(result, STPDecision)
        assert result.payment_schedule.discount_taken is False


# ---------------------------------------------------------------------------
# Each individual FR-3.1 sub-condition failing → EXCEPTION
# ---------------------------------------------------------------------------


class TestEachConditionFailsIndividually:
    """
    FR-3.1 requires ALL conditions to be true.
    Each condition failing alone must produce an EXCEPTION (not STP).
    """

    def test_vendor_unknown_routes_exception(self):
        result = route(_match(vendor_known=False), _invoice())
        assert isinstance(result, ExceptionDecision)

    def test_po_not_found_routes_exception(self):
        result = route(_match(po_resolved=False), _invoice())
        assert isinstance(result, ExceptionDecision)

    def test_contract_not_found_routes_exception(self):
        result = route(_match(contract_resolved=False), _invoice())
        assert isinstance(result, ExceptionDecision)

    def test_qty_mismatch_routes_exception(self):
        result = route(_match(quantities_match=False), _invoice())
        assert isinstance(result, ExceptionDecision)

    def test_price_variance_routes_exception(self):
        result = route(_match(prices_match=False), _invoice())
        assert isinstance(result, ExceptionDecision)

    def test_total_mismatch_routes_exception(self):
        result = route(_match(total_matches=False), _invoice())
        assert isinstance(result, ExceptionDecision)

    def test_missing_approval_routes_exception(self):
        result = route(_match(approval_satisfied=False), _invoice())
        assert isinstance(result, ExceptionDecision)


# ---------------------------------------------------------------------------
# Reason code mapping — correct code for each failing condition
# ---------------------------------------------------------------------------


class TestReasonCodeMapping:
    """
    Each failing FR-3.1 sub-condition must map to exactly the right
    ExceptionReasonCode (FR-4.2).
    """

    def _reason_codes(self, result: ExceptionDecision) -> set[ExceptionReasonCode]:
        return {r.reason_code for r in result.exception_record.reasons}

    def test_vendor_unknown_produces_unknown_vendor(self):
        result = route(_match(vendor_known=False), _invoice())
        assert isinstance(result, ExceptionDecision)
        assert ExceptionReasonCode.UNKNOWN_VENDOR in self._reason_codes(result)

    def test_po_not_found_produces_po_not_found(self):
        result = route(_match(po_resolved=False), _invoice())
        assert isinstance(result, ExceptionDecision)
        assert ExceptionReasonCode.PO_NOT_FOUND in self._reason_codes(result)

    def test_contract_not_found_produces_contract_not_found(self):
        result = route(_match(contract_resolved=False), _invoice())
        assert isinstance(result, ExceptionDecision)
        assert ExceptionReasonCode.CONTRACT_NOT_FOUND in self._reason_codes(result)

    def test_qty_mismatch_produces_qty_mismatch(self):
        detail = _line_detail(billed_qty="12", ordered_qty="10", qty_match=False)
        result = route(_match(quantities_match=False, line_details=[detail]), _invoice())
        assert isinstance(result, ExceptionDecision)
        assert ExceptionReasonCode.QTY_MISMATCH in self._reason_codes(result)

    def test_price_variance_produces_price_variance(self):
        detail = _line_detail(billed_price="42.00", contract_price="38.00", price_match=False)
        result = route(_match(prices_match=False, line_details=[detail]), _invoice())
        assert isinstance(result, ExceptionDecision)
        assert ExceptionReasonCode.PRICE_VARIANCE in self._reason_codes(result)

    def test_total_mismatch_produces_total_mismatch(self):
        result = route(
            _match(total_matches=False, total_variance_abs="5", total_variance_pct="0.012"),
            _invoice(),
        )
        assert isinstance(result, ExceptionDecision)
        assert ExceptionReasonCode.TOTAL_MISMATCH in self._reason_codes(result)

    def test_missing_approval_produces_missing_approval(self):
        result = route(_match(approval_satisfied=False), _invoice())
        assert isinstance(result, ExceptionDecision)
        assert ExceptionReasonCode.MISSING_APPROVAL in self._reason_codes(result)


# ---------------------------------------------------------------------------
# Supporting data attached to reason codes
# ---------------------------------------------------------------------------


class TestReasonSupportingData:
    def test_price_variance_supporting_data_contains_line_variances(self):
        detail = _line_detail(
            line_number=1,
            billed_price="42.00",
            contract_price="38.00",
            price_match=False,
        )
        result = route(_match(prices_match=False, line_details=[detail]), _invoice())
        assert isinstance(result, ExceptionDecision)
        price_reason = next(
            r for r in result.exception_record.reasons
            if r.reason_code == ExceptionReasonCode.PRICE_VARIANCE
        )
        assert "line_variances" in price_reason.supporting_data
        lv = price_reason.supporting_data["line_variances"]
        assert len(lv) == 1
        assert lv[0]["billed_unit_price"] == "42.00"
        assert lv[0]["contract_unit_price"] == "38.00"

    def test_qty_mismatch_supporting_data_contains_line_variances(self):
        detail = _line_detail(
            line_number=1,
            billed_qty="12",
            ordered_qty="10",
            qty_match=False,
        )
        result = route(_match(quantities_match=False, line_details=[detail]), _invoice())
        assert isinstance(result, ExceptionDecision)
        qty_reason = next(
            r for r in result.exception_record.reasons
            if r.reason_code == ExceptionReasonCode.QTY_MISMATCH
        )
        assert "line_variances" in qty_reason.supporting_data
        lv = qty_reason.supporting_data["line_variances"]
        assert lv[0]["billed_qty"] == "12"
        assert lv[0]["ordered_qty"] == "10"

    def test_total_mismatch_supporting_data_has_variance(self):
        result = route(
            _match(total_matches=False, total_variance_abs="5.00", total_variance_pct="0.0119"),
            _invoice(),
        )
        assert isinstance(result, ExceptionDecision)
        total_reason = next(
            r for r in result.exception_record.reasons
            if r.reason_code == ExceptionReasonCode.TOTAL_MISMATCH
        )
        assert "total_variance_abs" in total_reason.supporting_data


# ---------------------------------------------------------------------------
# Multiple simultaneous failures
# ---------------------------------------------------------------------------


class TestMultipleFailures:
    def test_two_failures_produce_two_reason_codes(self):
        result = route(
            _match(vendor_known=False, prices_match=False),
            _invoice(),
        )
        assert isinstance(result, ExceptionDecision)
        codes = {r.reason_code for r in result.exception_record.reasons}
        assert ExceptionReasonCode.UNKNOWN_VENDOR in codes
        assert ExceptionReasonCode.PRICE_VARIANCE in codes
        assert len(result.exception_record.reasons) == 2

    def test_all_conditions_failing_produces_all_reason_codes(self):
        result = route(
            _match(
                vendor_known=False,
                po_resolved=False,
                contract_resolved=False,
                quantities_match=False,
                prices_match=False,
                total_matches=False,
                approval_satisfied=False,
            ),
            _invoice(),
        )
        assert isinstance(result, ExceptionDecision)
        assert len(result.exception_record.reasons) == 7

    def test_exception_record_starts_open(self):
        result = route(_match(vendor_known=False), _invoice())
        assert isinstance(result, ExceptionDecision)
        assert result.exception_record.status == ExceptionStatus.OPEN

    def test_exception_record_has_no_resolution_fields(self):
        result = route(_match(vendor_known=False), _invoice())
        assert isinstance(result, ExceptionDecision)
        assert result.exception_record.human_action is None
        assert result.exception_record.actor_id is None


# ---------------------------------------------------------------------------
# Exception record never carries a PaymentSchedule
# ---------------------------------------------------------------------------


class TestExceptionNeverScheduled:
    """FR-4.1 — structural: ExceptionDecision has no payment_schedule attribute."""

    def test_exception_decision_has_no_payment_schedule(self):
        result = route(_match(vendor_known=False), _invoice())
        assert isinstance(result, ExceptionDecision)
        assert not hasattr(result, "payment_schedule"), (
            "ExceptionDecision must never carry a payment_schedule. "
            "FR-4.1: exceptions are never auto-paid."
        )

    def test_exception_record_invoice_id_matches(self):
        result = route(_match(invoice_id="INV-XYZ", vendor_known=False), _invoice())
        assert isinstance(result, ExceptionDecision)
        assert result.invoice_id == "INV-XYZ"
        assert result.exception_record.invoice_id == "INV-XYZ"


# ---------------------------------------------------------------------------
# Scenario 1 — clean invoice → STP
# ---------------------------------------------------------------------------


class TestScenario01Full:
    """
    requirements.md §8 scenario 1: fully matches PO and contract.
    Expected: STPDecision, PaymentSchedule present, correct amount.
    """

    def test_clean_invoice_routes_stp(self):
        result = route(_match(), _invoice())
        assert isinstance(result, STPDecision)
        assert result.outcome == "STP"

    def test_clean_invoice_schedule_amount(self):
        result = route(_match(), _invoice(grand_total="420.00"))
        assert isinstance(result, STPDecision)
        assert result.payment_schedule.amount == Decimal("420.00")

    def test_clean_invoice_no_human_step(self):
        """STPDecision has no exception_record — no human step required."""
        result = route(_match(), _invoice())
        assert isinstance(result, STPDecision)
        assert not hasattr(result, "exception_record")


# ---------------------------------------------------------------------------
# Scenario 2 — price variance → EXCEPTION with PRICE_VARIANCE
# ---------------------------------------------------------------------------


class TestScenario02Full:
    """
    requirements.md §8 scenario 2: $42/unit billed vs $38 contract.
    Expected: ExceptionDecision, PRICE_VARIANCE reason, variance quantified.
    """

    def _build(self) -> ExceptionDecision:
        detail = _line_detail(billed_price="42.00", contract_price="38.00", price_match=False)
        result = route(_match(prices_match=False, line_details=[detail]), _invoice())
        assert isinstance(result, ExceptionDecision)
        return result

    def test_routes_exception(self):
        result = self._build()
        assert result.outcome == "EXCEPTION"

    def test_reason_is_price_variance(self):
        result = self._build()
        codes = {r.reason_code for r in result.exception_record.reasons}
        assert ExceptionReasonCode.PRICE_VARIANCE in codes

    def test_variance_quantified_in_supporting_data(self):
        result = self._build()
        price_r = next(
            r for r in result.exception_record.reasons
            if r.reason_code == ExceptionReasonCode.PRICE_VARIANCE
        )
        lv = price_r.supporting_data["line_variances"][0]
        assert lv["price_variance_abs"] == "4.00"

    def test_not_scheduled(self):
        result = self._build()
        assert not hasattr(result, "payment_schedule")


# ---------------------------------------------------------------------------
# Scenario 3 — missing approval → EXCEPTION with MISSING_APPROVAL
# ---------------------------------------------------------------------------


class TestScenario03Full:
    """
    requirements.md §8 scenario 3: over $10k threshold, no approval.
    Expected: ExceptionDecision, MISSING_APPROVAL reason.
    """

    def _build(self) -> ExceptionDecision:
        result = route(_match(approval_satisfied=False), _invoice(grand_total="12000.00"))
        assert isinstance(result, ExceptionDecision)
        return result

    def test_routes_exception(self):
        assert self._build().outcome == "EXCEPTION"

    def test_reason_is_missing_approval(self):
        result = self._build()
        codes = {r.reason_code for r in result.exception_record.reasons}
        assert ExceptionReasonCode.MISSING_APPROVAL in codes

    def test_not_scheduled(self):
        assert not hasattr(self._build(), "payment_schedule")


# ---------------------------------------------------------------------------
# Scenario 6 — unknown vendor → EXCEPTION with UNKNOWN_VENDOR
# ---------------------------------------------------------------------------


class TestScenario06Full:
    """
    requirements.md §8 scenario 6: vendor not in vendor master.
    Expected: ExceptionDecision, UNKNOWN_VENDOR reason, regardless of other checks.
    """

    def _build(self) -> ExceptionDecision:
        result = route(_match(vendor_known=False), _invoice())
        assert isinstance(result, ExceptionDecision)
        return result

    def test_routes_exception(self):
        assert self._build().outcome == "EXCEPTION"

    def test_reason_is_unknown_vendor(self):
        codes = {r.reason_code for r in self._build().exception_record.reasons}
        assert ExceptionReasonCode.UNKNOWN_VENDOR in codes

    def test_not_scheduled_even_if_prices_qty_match(self):
        # All other checks pass, only vendor_known=False.
        result = route(_match(vendor_known=False), _invoice())
        assert not hasattr(result, "payment_schedule")


# ---------------------------------------------------------------------------
# Scenario 7 — PO not found → EXCEPTION with PO_NOT_FOUND
# ---------------------------------------------------------------------------


class TestScenario07Full:
    """
    requirements.md §8 scenario 7: PO reference doesn't resolve.
    Expected: ExceptionDecision, PO_NOT_FOUND reason. No silent failure.
    """

    def _build(self) -> ExceptionDecision:
        result = route(_match(po_resolved=False), _invoice())
        assert isinstance(result, ExceptionDecision)
        return result

    def test_routes_exception(self):
        assert self._build().outcome == "EXCEPTION"

    def test_reason_is_po_not_found(self):
        codes = {r.reason_code for r in self._build().exception_record.reasons}
        assert ExceptionReasonCode.PO_NOT_FOUND in codes

    def test_not_scheduled(self):
        assert not hasattr(self._build(), "payment_schedule")


# ---------------------------------------------------------------------------
# Human-gate API endpoints (FastAPI TestClient)
# ---------------------------------------------------------------------------


class TestHumanGateEndpoints:
    """
    Tests for POST /exceptions/{invoice_id}/approve and /reject.
    FR-4.3: actions must be attributed (actor_id required).
    """

    def setup_method(self):
        """Register a fresh exception before each test."""
        clear_store()
        decision = route(_match(vendor_known=False, invoice_id="INV-GATE-001"), _invoice())
        assert isinstance(decision, ExceptionDecision)
        register_exception(decision)

    def teardown_method(self):
        clear_store()

    # --- GET ---

    def test_get_exception_returns_200(self):
        resp = client.get("/exceptions/INV-GATE-001")
        assert resp.status_code == 200

    def test_get_exception_returns_reason_codes(self):
        resp = client.get("/exceptions/INV-GATE-001")
        data = resp.json()
        codes = [r["reason_code"] for r in data["reasons"]]
        assert "UNKNOWN_VENDOR" in codes

    def test_get_exception_404_for_unknown(self):
        resp = client.get("/exceptions/NO-SUCH-INVOICE")
        assert resp.status_code == 404

    # --- APPROVE ---

    def test_approve_returns_200(self):
        resp = client.post(
            "/exceptions/INV-GATE-001/approve",
            json={"human_action": "APPROVE_OVERRIDE", "actor_id": "clerk-001"},
        )
        assert resp.status_code == 200

    def test_approve_response_carries_actor_id(self):
        resp = client.post(
            "/exceptions/INV-GATE-001/approve",
            json={"human_action": "APPROVE_OVERRIDE", "actor_id": "clerk-001"},
        )
        data = resp.json()
        assert data["actor_id"] == "clerk-001"

    def test_approve_response_status_resolved(self):
        resp = client.post(
            "/exceptions/INV-GATE-001/approve",
            json={"human_action": "APPROVE_OVERRIDE", "actor_id": "clerk-001"},
        )
        assert resp.json()["status"] == "RESOLVED"

    def test_approve_response_includes_reason_codes(self):
        resp = client.post(
            "/exceptions/INV-GATE-001/approve",
            json={"human_action": "APPROVE_OVERRIDE", "actor_id": "clerk-001"},
        )
        data = resp.json()
        assert "UNKNOWN_VENDOR" in data["reason_codes"]

    def test_approve_wrong_action_returns_422(self):
        resp = client.post(
            "/exceptions/INV-GATE-001/approve",
            json={"human_action": "REJECT", "actor_id": "clerk-001"},
        )
        assert resp.status_code == 422

    def test_approve_unknown_invoice_returns_404(self):
        resp = client.post(
            "/exceptions/NO-SUCH/approve",
            json={"human_action": "APPROVE_OVERRIDE", "actor_id": "clerk-001"},
        )
        assert resp.status_code == 404

    def test_approve_with_resolution_notes(self):
        resp = client.post(
            "/exceptions/INV-GATE-001/approve",
            json={
                "human_action": "APPROVE_OVERRIDE",
                "actor_id": "clerk-001",
                "resolution_notes": "Vendor confirmed correct — updating master.",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["resolution_notes"] == "Vendor confirmed correct — updating master."

    # --- REJECT ---

    def test_reject_returns_200(self):
        resp = client.post(
            "/exceptions/INV-GATE-001/reject",
            json={"human_action": "REJECT", "actor_id": "clerk-002"},
        )
        assert resp.status_code == 200

    def test_reject_response_action_is_reject(self):
        resp = client.post(
            "/exceptions/INV-GATE-001/reject",
            json={"human_action": "REJECT", "actor_id": "clerk-002"},
        )
        assert resp.json()["human_action"] == "REJECT"

    def test_reject_wrong_action_returns_422(self):
        resp = client.post(
            "/exceptions/INV-GATE-001/reject",
            json={"human_action": "APPROVE_OVERRIDE", "actor_id": "clerk-002"},
        )
        assert resp.status_code == 422

    def test_reject_unknown_invoice_returns_404(self):
        resp = client.post(
            "/exceptions/NO-SUCH/reject",
            json={"human_action": "REJECT", "actor_id": "clerk-002"},
        )
        assert resp.status_code == 404

    def test_attribution_required_blank_actor_id_rejected(self):
        """actor_id must not be blank — anonymous actions are prohibited (FR-4.3)."""
        resp = client.post(
            "/exceptions/INV-GATE-001/approve",
            json={"human_action": "APPROVE_OVERRIDE", "actor_id": "   "},
        )
        # Pydantic validation on HumanResolutionUpdate rejects blank actor_id.
        assert resp.status_code == 422
