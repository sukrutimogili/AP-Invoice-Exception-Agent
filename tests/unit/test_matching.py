"""
tests/unit/test_matching.py — Phase 3 unit tests for the matching engine.

spec.md Phase 3 testing requirement — every branch listed:
  - full match (all checks pass) → overall_passed = True
  - qty mismatch on one line
  - price variance ABOVE tolerance → prices_match = False
  - price variance WITHIN tolerance → prices_match = True
  - missing PO (po_resolved = False)
  - missing contract (contract_resolved = False)
  - unknown vendor (vendor_known = False)
  - inactive vendor (vendor_known = False)
  - over-threshold WITHOUT approval → approval_satisfied = False
  - over-threshold WITH approval → approval_satisfied = True
  - under-threshold → approval_satisfied = True regardless of approval flag

requirements.md §8 scenario coverage (deterministic matching layer):
  Scenario 2 — unit-price variance → PRICE_VARIANCE exception
  Scenario 3 — over threshold, no approval → MISSING_APPROVAL exception
  Scenario 6 — unknown vendor → UNKNOWN_VENDOR exception
  Scenario 7 — PO not found → PO_NOT_FOUND exception

All tests use pure Pydantic objects — no DB, no network, no LLM.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from matching.engine import MatchInput, MatchingEngine, _check_approval, _check_vendor
from matching.tolerance import compute_variance, within_tolerance
from models.contract import ContractCreate, ContractLineItemCreate
from models.invoice import InvoiceCreate, InvoiceLineItemCreate
from models.match_result import MatchResultCreate
from models.purchase_order import POLineItemCreate, PurchaseOrderCreate
from models.vendor import VendorCreate


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

_ENGINE = MatchingEngine()

# Default tolerance: exact match (0.0 pp).
_ZERO_TOL = Decimal("0")
# A loose tolerance: ±5%.
_FIVE_PCT_TOL = Decimal("5")


def _make_vendor(*, is_active: bool = True, name: str = "Acme Supplies Ltd") -> VendorCreate:
    return VendorCreate(
        vendor_code="ACME-001",
        name=name,
        is_active=is_active,
    )


def _make_po(
    *,
    po_number: str = "PO-2025-0100",
    po_total: str = "420.00",
    approval_threshold: str = "10000.00",
    line_items: list[dict] | None = None,
) -> PurchaseOrderCreate:
    if line_items is None:
        line_items = [
            {"line_number": 1, "description": "Widget Type A", "qty": "10", "unit_price": "38.00"},
            {"line_number": 2, "description": "Shipping & Handling", "qty": "1", "unit_price": "20.00"},
        ]
    return PurchaseOrderCreate(
        po_number=po_number,
        vendor_id="vendor-uuid-001",
        po_total=Decimal(po_total),
        approval_threshold=Decimal(approval_threshold),
        line_items=[POLineItemCreate(**li) for li in line_items],
    )


def _make_contract(
    *,
    contract_reference: str = "CTR-2025-0018",
    line_items: list[dict] | None = None,
) -> ContractCreate:
    if line_items is None:
        line_items = [
            {"line_number": 1, "description": "Widget Type A", "unit_price": "38.00"},
            {"line_number": 2, "description": "Shipping & Handling", "unit_price": "20.00"},
        ]
    return ContractCreate(
        contract_reference=contract_reference,
        vendor_id="vendor-uuid-001",
        line_items=[ContractLineItemCreate(**li) for li in line_items],
    )


def _make_invoice(
    *,
    grand_total: str = "420.00",
    subtotal: str = "400.00",
    tax: str = "20.00",
    line_items: list[dict] | None = None,
    invoice_number: str = "INV-2026-0042",
    vendor_name: str = "Acme Supplies Ltd",
    po_reference: str = "PO-2025-0100",
    contract_reference: str = "CTR-2025-0018",
) -> InvoiceCreate:
    if line_items is None:
        line_items = [
            {
                "line_number": 1,
                "description": "Widget Type A",
                "qty": "10",
                "unit_price": "38.00",
                "amount": "380.00",
            },
            {
                "line_number": 2,
                "description": "Shipping & Handling",
                "qty": "1",
                "unit_price": "20.00",
                "amount": "20.00",
            },
        ]
    return InvoiceCreate(
        invoice_number=invoice_number,
        vendor_name=vendor_name,
        invoice_date="2026-01-15",
        po_reference=po_reference,
        contract_reference=contract_reference,
        subtotal=Decimal(subtotal),
        tax=Decimal(tax),
        grand_total=Decimal(grand_total),
        due_date="2026-02-14",
        payment_terms="Net 30",
        line_items=[InvoiceLineItemCreate(**li) for li in line_items],
    )


def _make_input(**overrides) -> MatchInput:
    """Return a fully-valid MatchInput; override individual fields as needed."""
    defaults: dict = dict(
        invoice=_make_invoice(),
        purchase_order=_make_po(),
        contract=_make_contract(),
        vendor=_make_vendor(),
        approval_on_file=False,
        tolerance_pct=_ZERO_TOL,
        invoice_id="INV-2026-0042",
    )
    defaults.update(overrides)
    return MatchInput(**defaults)


# ---------------------------------------------------------------------------
# tolerance.py — unit tests
# ---------------------------------------------------------------------------


class TestComputeVariance:
    def test_zero_variance(self):
        abs_v, pct_v = compute_variance(Decimal("100"), Decimal("100"))
        assert abs_v == Decimal("0")
        assert pct_v == Decimal("0")

    def test_positive_variance(self):
        # Billed $42 vs reference $38 → +$4, +10.5263...%
        abs_v, pct_v = compute_variance(Decimal("42"), Decimal("38"))
        assert abs_v == Decimal("4")
        assert pct_v > Decimal("0.10")  # just above 10%

    def test_negative_variance(self):
        abs_v, pct_v = compute_variance(Decimal("36"), Decimal("38"))
        assert abs_v == Decimal("-2")
        assert pct_v < Decimal("0")

    def test_zero_reference_nonzero_billed(self):
        _, pct_v = compute_variance(Decimal("5"), Decimal("0"))
        assert pct_v == Decimal("1")  # treated as 100%

    def test_zero_reference_zero_billed(self):
        _, pct_v = compute_variance(Decimal("0"), Decimal("0"))
        assert pct_v == Decimal("0")


class TestWithinTolerance:
    def test_exact_match_passes_zero_tolerance(self):
        assert within_tolerance(Decimal("0"), Decimal("0")) is True

    def test_tiny_variance_fails_zero_tolerance(self):
        assert within_tolerance(Decimal("0.001"), Decimal("0")) is False

    def test_variance_within_2pct_tolerance(self):
        # 1.5% variance, 2% tolerance → pass
        assert within_tolerance(Decimal("0.015"), Decimal("2")) is True

    def test_variance_exactly_at_boundary(self):
        # 2% variance, 2% tolerance → pass (≤, not <)
        assert within_tolerance(Decimal("0.02"), Decimal("2")) is True

    def test_variance_above_tolerance(self):
        # 2.01% variance, 2% tolerance → fail
        assert within_tolerance(Decimal("0.0201"), Decimal("2")) is False

    def test_negative_variance_within_tolerance(self):
        # −1% variance is within 2% tolerance
        assert within_tolerance(Decimal("-0.01"), Decimal("2")) is True


# ---------------------------------------------------------------------------
# Full match — all checks pass (Scenario 1 matching layer)
# ---------------------------------------------------------------------------


class TestFullMatch:
    def test_overall_passed_is_true(self):
        result = _ENGINE.run(_make_input())
        assert result.overall_passed is True

    def test_all_sub_checks_true(self):
        result = _ENGINE.run(_make_input())
        assert result.vendor_known is True
        assert result.po_resolved is True
        assert result.contract_resolved is True
        assert result.quantities_match is True
        assert result.prices_match is True
        assert result.total_matches is True
        assert result.approval_satisfied is True

    def test_line_details_present(self):
        result = _ENGINE.run(_make_input())
        assert len(result.line_item_details) == 2

    def test_line_detail_qty_match(self):
        result = _ENGINE.run(_make_input())
        for detail in result.line_item_details:
            assert detail.qty_match is True

    def test_line_detail_price_match(self):
        result = _ENGINE.run(_make_input())
        for detail in result.line_item_details:
            assert detail.price_match is True

    def test_total_variance_is_zero(self):
        result = _ENGINE.run(_make_input())
        assert result.total_variance_abs == Decimal("0")


# ---------------------------------------------------------------------------
# Qty mismatch
# ---------------------------------------------------------------------------


class TestQtyMismatch:
    """FR-2.2: quantity billed ≠ quantity ordered."""

    def test_qty_mismatch_sets_quantities_match_false(self):
        # Billed qty=12, ordered qty=10 on line 1.
        invoice = _make_invoice(
            line_items=[
                {"line_number": 1, "description": "Widget Type A", "qty": "12", "unit_price": "38.00", "amount": "456.00"},
                {"line_number": 2, "description": "Shipping & Handling", "qty": "1", "unit_price": "20.00", "amount": "20.00"},
            ],
            grand_total="476.00",
            subtotal="456.00",
        )
        result = _ENGINE.run(_make_input(invoice=invoice))
        assert result.quantities_match is False
        assert result.overall_passed is False

    def test_qty_mismatch_recorded_in_line_detail(self):
        invoice = _make_invoice(
            line_items=[
                {"line_number": 1, "description": "Widget Type A", "qty": "12", "unit_price": "38.00", "amount": "456.00"},
                {"line_number": 2, "description": "Shipping & Handling", "qty": "1", "unit_price": "20.00", "amount": "20.00"},
            ],
            grand_total="476.00",
            subtotal="456.00",
        )
        result = _ENGINE.run(_make_input(invoice=invoice))
        line1 = next(d for d in result.line_item_details if d.line_number == 1)
        assert line1.qty_match is False
        assert line1.qty_variance_abs == Decimal("2")  # 12 − 10

    def test_exact_qty_match_passes(self):
        result = _ENGINE.run(_make_input())
        assert result.quantities_match is True


# ---------------------------------------------------------------------------
# Scenario 2 — unit-price variance (requirements.md §8)
# ---------------------------------------------------------------------------


class TestScenario02PriceVariance:
    """
    Scenario 2: billed $42/unit vs $38 contract.
    Expected: prices_match = False, overall_passed = False.
    Price variance quantified in line detail.
    """

    def _scenario2_invoice(self) -> InvoiceCreate:
        """Billed $42/unit on line 1 vs $38 contract."""
        return _make_invoice(
            line_items=[
                # $42 billed, $38 contracted → +$4, +10.53%
                {"line_number": 1, "description": "Widget Type A", "qty": "10", "unit_price": "42.00", "amount": "420.00"},
                {"line_number": 2, "description": "Shipping & Handling", "qty": "1", "unit_price": "20.00", "amount": "20.00"},
            ],
            subtotal="440.00",
            grand_total="440.00",
        )

    def test_price_variance_above_zero_tolerance_fails(self):
        result = _ENGINE.run(_make_input(invoice=self._scenario2_invoice(), tolerance_pct=_ZERO_TOL))
        assert result.prices_match is False
        assert result.overall_passed is False

    def test_price_variance_within_15pct_tolerance_passes(self):
        """With 15% tolerance, 10.53% variance should pass."""
        result = _ENGINE.run(_make_input(
            invoice=self._scenario2_invoice(),
            tolerance_pct=Decimal("15"),
        ))
        assert result.prices_match is True

    def test_price_variance_quantified_correctly(self):
        result = _ENGINE.run(_make_input(invoice=self._scenario2_invoice(), tolerance_pct=_ZERO_TOL))
        line1 = next(d for d in result.line_item_details if d.line_number == 1)
        assert line1.price_variance_abs == Decimal("4")  # 42 − 38
        assert line1.billed_unit_price == Decimal("42")
        assert line1.contract_unit_price == Decimal("38")
        assert line1.price_match is False

    def test_line2_price_still_passes(self):
        """Only line 1 has the variance; line 2 should still match."""
        result = _ENGINE.run(_make_input(invoice=self._scenario2_invoice(), tolerance_pct=_ZERO_TOL))
        line2 = next(d for d in result.line_item_details if d.line_number == 2)
        assert line2.price_match is True

    def test_price_variance_exactly_at_boundary_passes(self):
        """Variance exactly at tolerance boundary → should pass (≤ not <)."""
        # 42/38 - 1 ≈ 10.526% variance; set tolerance to 10.53% (approx)
        result = _ENGINE.run(_make_input(
            invoice=self._scenario2_invoice(),
            tolerance_pct=Decimal("10.53"),
        ))
        assert result.prices_match is True


# ---------------------------------------------------------------------------
# Scenario 3 — missing approval (requirements.md §8)
# ---------------------------------------------------------------------------


class TestScenario03MissingApproval:
    """
    Scenario 3: invoice over $10k threshold, no approval on file.
    Expected: approval_satisfied = False, overall_passed = False.
    """

    def _over_threshold_invoice(self) -> InvoiceCreate:
        return _make_invoice(
            grand_total="12000.00",
            subtotal="11000.00",
            tax="1000.00",
            line_items=[
                {"line_number": 1, "description": "Bulk Widget Order", "qty": "100", "unit_price": "110.00", "amount": "11000.00"},
            ],
        )

    def _over_threshold_po(self) -> PurchaseOrderCreate:
        return _make_po(
            po_total="12000.00",
            approval_threshold="10000.00",
            line_items=[
                {"line_number": 1, "description": "Bulk Widget Order", "qty": "100", "unit_price": "110.00"},
            ],
        )

    def _over_threshold_contract(self) -> ContractCreate:
        return _make_contract(
            line_items=[
                {"line_number": 1, "description": "Bulk Widget Order", "unit_price": "110.00"},
            ],
        )

    def test_over_threshold_no_approval_fails(self):
        result = _ENGINE.run(_make_input(
            invoice=self._over_threshold_invoice(),
            purchase_order=self._over_threshold_po(),
            contract=self._over_threshold_contract(),
            approval_on_file=False,
        ))
        assert result.approval_satisfied is False
        assert result.overall_passed is False

    def test_over_threshold_with_approval_passes(self):
        result = _ENGINE.run(_make_input(
            invoice=self._over_threshold_invoice(),
            purchase_order=self._over_threshold_po(),
            contract=self._over_threshold_contract(),
            approval_on_file=True,
        ))
        assert result.approval_satisfied is True
        assert result.overall_passed is True

    def test_under_threshold_no_approval_passes(self):
        """Under threshold → approval not required → approval_satisfied = True."""
        result = _ENGINE.run(_make_input(approval_on_file=False))
        # Default invoice is $420, threshold is $10,000.
        assert result.approval_satisfied is True

    def test_exactly_at_threshold_requires_approval(self):
        """grand_total == approval_threshold → approval required (FR-2.4: 'at or above')."""
        invoice = _make_invoice(
            grand_total="10000.00",
            subtotal="9000.00",
            tax="1000.00",
            line_items=[
                {"line_number": 1, "description": "Widget Type A", "qty": "10", "unit_price": "900.00", "amount": "9000.00"},
            ],
        )
        po = _make_po(
            po_total="10000.00",
            approval_threshold="10000.00",
            line_items=[
                {"line_number": 1, "description": "Widget Type A", "qty": "10", "unit_price": "900.00"},
            ],
        )
        contract = _make_contract(
            line_items=[
                {"line_number": 1, "description": "Widget Type A", "unit_price": "900.00"},
            ],
        )
        # Without approval → fails.
        result = _ENGINE.run(_make_input(invoice=invoice, purchase_order=po, contract=contract, approval_on_file=False))
        assert result.approval_satisfied is False

        # With approval → passes.
        result = _ENGINE.run(_make_input(invoice=invoice, purchase_order=po, contract=contract, approval_on_file=True))
        assert result.approval_satisfied is True


# ---------------------------------------------------------------------------
# Scenario 6 — unknown / inactive vendor (requirements.md §8)
# ---------------------------------------------------------------------------


class TestScenario06UnknownVendor:
    """
    Scenario 6: vendor not in vendor master (or not active).
    Expected: vendor_known = False, overall_passed = False — regardless of price/qty match.
    """

    def test_vendor_none_fails(self):
        result = _ENGINE.run(_make_input(vendor=None))
        assert result.vendor_known is False
        assert result.overall_passed is False

    def test_inactive_vendor_fails(self):
        result = _ENGINE.run(_make_input(vendor=_make_vendor(is_active=False)))
        assert result.vendor_known is False
        assert result.overall_passed is False

    def test_unknown_vendor_fails_even_if_prices_match(self):
        """FR-2.5: unknown vendors are always an exception regardless of match quality."""
        result = _ENGINE.run(_make_input(vendor=None))
        # All other checks still reflect the data.
        assert result.po_resolved is True
        assert result.prices_match is True
        # But overall still fails.
        assert result.overall_passed is False

    def test_active_vendor_passes(self):
        result = _ENGINE.run(_make_input(vendor=_make_vendor(is_active=True)))
        assert result.vendor_known is True


# ---------------------------------------------------------------------------
# Scenario 7 — PO not found (requirements.md §8)
# ---------------------------------------------------------------------------


class TestScenario07PONotFound:
    """
    Scenario 7: PO reference doesn't resolve.
    Expected: po_resolved = False, overall_passed = False. No silent failure.
    """

    def test_po_none_sets_po_resolved_false(self):
        result = _ENGINE.run(_make_input(purchase_order=None))
        assert result.po_resolved is False
        assert result.overall_passed is False

    def test_po_none_cascades_to_qty_and_total(self):
        """Without a PO there are no ordered quantities or a PO total to compare."""
        result = _ENGINE.run(_make_input(purchase_order=None))
        assert result.quantities_match is False
        assert result.total_matches is False

    def test_po_none_total_variance_is_none(self):
        result = _ENGINE.run(_make_input(purchase_order=None))
        assert result.total_variance_abs is None
        assert result.total_variance_pct is None

    def test_contract_none_sets_contract_resolved_false(self):
        result = _ENGINE.run(_make_input(contract=None))
        assert result.contract_resolved is False
        assert result.prices_match is False
        assert result.overall_passed is False

    def test_both_po_and_contract_none(self):
        result = _ENGINE.run(_make_input(purchase_order=None, contract=None))
        assert result.po_resolved is False
        assert result.contract_resolved is False
        assert result.overall_passed is False


# ---------------------------------------------------------------------------
# Total mismatch
# ---------------------------------------------------------------------------


class TestTotalMismatch:
    """FR-2.3: invoice grand_total vs PO total."""

    def test_total_mismatch_fails_zero_tolerance(self):
        # Invoice $425, PO $420 → +$5 / +1.19% → fails at 0% tolerance.
        invoice = _make_invoice(grand_total="425.00", subtotal="400.00", tax="25.00")
        result = _ENGINE.run(_make_input(invoice=invoice))
        assert result.total_matches is False
        assert result.overall_passed is False

    def test_total_variance_recorded(self):
        invoice = _make_invoice(grand_total="425.00", subtotal="400.00", tax="25.00")
        result = _ENGINE.run(_make_input(invoice=invoice))
        assert result.total_variance_abs == Decimal("5")  # 425 − 420

    def test_total_mismatch_within_tolerance_passes(self):
        # 5/420 ≈ 1.19% — passes with 2% tolerance.
        invoice = _make_invoice(grand_total="425.00", subtotal="400.00", tax="25.00")
        result = _ENGINE.run(_make_input(invoice=invoice, tolerance_pct=Decimal("2")))
        assert result.total_matches is True


# ---------------------------------------------------------------------------
# Multiple simultaneous failures
# ---------------------------------------------------------------------------


class TestMultipleFailures:
    """overall_passed must be False when ANY sub-check fails."""

    def test_vendor_and_price_both_fail(self):
        invoice = _make_invoice(
            line_items=[
                {"line_number": 1, "description": "Widget Type A", "qty": "10", "unit_price": "42.00", "amount": "420.00"},
                {"line_number": 2, "description": "Shipping & Handling", "qty": "1", "unit_price": "20.00", "amount": "20.00"},
            ],
            subtotal="440.00",
            grand_total="440.00",
        )
        result = _ENGINE.run(_make_input(invoice=invoice, vendor=None))
        assert result.vendor_known is False
        assert result.prices_match is False
        assert result.overall_passed is False

    def test_all_checks_failing(self):
        result = _ENGINE.run(_make_input(
            invoice=_make_invoice(grand_total="9999.00", subtotal="9000.00", tax="999.00",
                                   line_items=[{"line_number": 1, "description": "X", "qty": "1", "unit_price": "9000.00", "amount": "9000.00"}]),
            purchase_order=None,
            contract=None,
            vendor=None,
        ))
        assert result.overall_passed is False
        assert result.vendor_known is False
        assert result.po_resolved is False
        assert result.contract_resolved is False


# ---------------------------------------------------------------------------
# MatchResultCreate type and content
# ---------------------------------------------------------------------------


class TestMatchResultShape:
    def test_returns_match_result_create(self):
        result = _ENGINE.run(_make_input())
        assert isinstance(result, MatchResultCreate)

    def test_invoice_id_set(self):
        result = _ENGINE.run(_make_input(invoice_id="TEST-ID-001"))
        assert result.invoice_id == "TEST-ID-001"

    def test_invoice_id_defaults_to_invoice_number(self):
        # MatchInput with no invoice_id → defaults to invoice.invoice_number
        inp = MatchInput(
            invoice=_make_invoice(invoice_number="INV-AUTO"),
            purchase_order=_make_po(),
            contract=_make_contract(),
            vendor=_make_vendor(),
        )
        result = _ENGINE.run(inp)
        assert result.invoice_id == "INV-AUTO"

    def test_line_item_count_matches_invoice(self):
        result = _ENGINE.run(_make_input())
        assert len(result.line_item_details) == len(_make_invoice().line_items)


# ---------------------------------------------------------------------------
# _check_vendor and _check_approval pure-function isolation tests
# ---------------------------------------------------------------------------


class TestCheckVendorIsolated:
    def test_none_returns_false(self):
        assert _check_vendor(None) is False

    def test_active_returns_true(self):
        assert _check_vendor(_make_vendor(is_active=True)) is True

    def test_inactive_returns_false(self):
        assert _check_vendor(_make_vendor(is_active=False)) is False


class TestCheckApprovalIsolated:
    def _inv(self, total: str) -> InvoiceCreate:
        """Minimal invoice with the given grand_total."""
        amount = Decimal(total) - Decimal("0")  # tax = 0
        return _make_invoice(
            grand_total=total,
            subtotal=total,
            tax="0.00",
            line_items=[
                {"line_number": 1, "description": "Item", "qty": "1", "unit_price": total, "amount": total}
            ],
        )

    def test_under_threshold_no_approval_needed(self):
        po = _make_po(approval_threshold="10000.00", po_total="500.00",
                      line_items=[{"line_number": 1, "description": "Item", "qty": "1", "unit_price": "500.00"}])
        inv = _make_invoice(grand_total="500.00", subtotal="500.00", tax="0.00",
                             line_items=[{"line_number": 1, "description": "Item", "qty": "1", "unit_price": "500.00", "amount": "500.00"}])
        assert _check_approval(inv, po, approval_on_file=False) is True

    def test_over_threshold_without_approval_fails(self):
        po = _make_po(approval_threshold="10000.00", po_total="15000.00",
                      line_items=[{"line_number": 1, "description": "Item", "qty": "1", "unit_price": "15000.00"}])
        inv = _make_invoice(grand_total="15000.00", subtotal="15000.00", tax="0.00",
                             line_items=[{"line_number": 1, "description": "Item", "qty": "1", "unit_price": "15000.00", "amount": "15000.00"}])
        assert _check_approval(inv, po, approval_on_file=False) is False

    def test_over_threshold_with_approval_passes(self):
        po = _make_po(approval_threshold="10000.00", po_total="15000.00",
                      line_items=[{"line_number": 1, "description": "Item", "qty": "1", "unit_price": "15000.00"}])
        inv = _make_invoice(grand_total="15000.00", subtotal="15000.00", tax="0.00",
                             line_items=[{"line_number": 1, "description": "Item", "qty": "1", "unit_price": "15000.00", "amount": "15000.00"}])
        assert _check_approval(inv, po, approval_on_file=True) is True

    def test_po_none_conservative_fail_without_approval(self):
        """When PO is None, threshold unknown → conservative fail."""
        inv = _make_invoice()
        assert _check_approval(inv, po=None, approval_on_file=False) is False

    def test_po_none_with_approval_passes(self):
        """When PO is None but approval is on file, trust the approval."""
        inv = _make_invoice()
        assert _check_approval(inv, po=None, approval_on_file=True) is True
