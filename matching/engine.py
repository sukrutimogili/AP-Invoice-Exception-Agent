"""
matching/engine.py — Deterministic matching engine.

spec.md Phase 3 / requirements.md FR-2:
  Pure business logic — no LLM calls, no I/O beyond what is passed in.
  Every check maps 1-to-1 to a FR-2.x sub-requirement.

FR-2.1  PO resolution   → po_resolved check
FR-2.1  Contract res.   → contract_resolved check
FR-2.2  Per-line qty and unit-price comparison → quantities_match, prices_match
FR-2.3  Grand-total comparison → total_matches
FR-2.4  Approval threshold → approval_satisfied
FR-2.5  Vendor master check → vendor_known
FR-2.6  Missing PO/contract → explicit exception (not silent)
FR-3.1  overall_passed = AND of all six sub-checks

The engine takes plain Pydantic objects and a tolerance value — no DB session,
no settings singleton.  The caller is responsible for resolving entities from
the database and passing the tolerance from config.

Design:
  - MatchInput  — typed input bundle (invoice + resolved entities + config).
  - MatchingEngine.run(input) → MatchResultCreate
  - Pure functions for each sub-check, testable in isolation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from models.contract import ContractCreate
from models.invoice import InvoiceCreate
from models.match_result import LineItemMatchDetail, MatchResultCreate
from models.purchase_order import PurchaseOrderCreate
from models.vendor import VendorCreate
from matching.tolerance import compute_variance, within_tolerance

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchInput:
    """
    All data needed by the matching engine for one invoice.

    Entities that could not be resolved are passed as None; the engine treats
    None as a resolution failure (FR-2.6) and sets the corresponding check
    to False without raising an exception.

    Args:
        invoice:           The validated extracted invoice (InvoiceCreate).
        purchase_order:    Resolved PO, or None if not found (FR-2.6).
        contract:          Resolved contract, or None if not found (FR-2.6).
        vendor:            Resolved vendor record, or None if not found (FR-2.5).
        approval_on_file:  True if a valid approval record exists for this
                           invoice (used by FR-2.4). Defaults to False.
        tolerance_pct:     MATCH_TOLERANCE_PERCENT from config (in percentage-
                           point units, e.g. 2.0 means ±2%). Default 0.0 =
                           exact match required.
        invoice_id:        Optional stable ID to embed in MatchResultCreate.
                           Defaults to invoice_number when not supplied.
    """

    invoice: InvoiceCreate
    purchase_order: PurchaseOrderCreate | None
    contract: ContractCreate | None
    vendor: VendorCreate | None
    approval_on_file: bool = False
    tolerance_pct: Decimal = Decimal("0")
    invoice_id: str = ""

    def __post_init__(self) -> None:
        # Default invoice_id to invoice_number if caller did not supply one.
        if not self.invoice_id:
            object.__setattr__(self, "invoice_id", self.invoice.invoice_number)


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------


class MatchingEngine:
    """
    Deterministic invoice matching engine.

    All checks are pure — no LLM calls, no database access, no side effects.
    Instantiate once (optionally with a logger); call run() per invoice.
    """

    def run(self, inp: MatchInput) -> MatchResultCreate:
        """
        Run all FR-2 checks against the invoice and return a MatchResultCreate.

        Args:
            inp: MatchInput bundle with the invoice, resolved entities, and config.

        Returns:
            MatchResultCreate with field-by-field results and overall_passed.
        """
        logger.info(
            "Matching started",
            extra={"invoice_id": inp.invoice_id},
        )

        # ---------------------------------------------------------------
        # FR-2.5  Vendor check
        # ---------------------------------------------------------------
        vendor_known = _check_vendor(inp.vendor)

        # ---------------------------------------------------------------
        # FR-2.6  PO / Contract resolution
        # ---------------------------------------------------------------
        po_resolved = inp.purchase_order is not None
        contract_resolved = inp.contract is not None

        # ---------------------------------------------------------------
        # FR-2.2  Per-line qty and unit-price comparison
        # ---------------------------------------------------------------
        line_details, quantities_match, prices_match = _check_line_items(
            inp.invoice,
            inp.purchase_order,
            inp.contract,
            inp.tolerance_pct,
        )

        # ---------------------------------------------------------------
        # FR-2.3  Grand-total comparison
        # ---------------------------------------------------------------
        total_matches, total_variance_abs, total_variance_pct = _check_total(
            inp.invoice,
            inp.purchase_order,
            inp.tolerance_pct,
        )

        # ---------------------------------------------------------------
        # FR-2.4  Approval threshold
        # ---------------------------------------------------------------
        approval_satisfied = _check_approval(
            inp.invoice,
            inp.purchase_order,
            inp.approval_on_file,
        )

        # ---------------------------------------------------------------
        # FR-3.1  Overall result = AND of all checks
        # ---------------------------------------------------------------
        overall_passed = (
            vendor_known
            and po_resolved
            and contract_resolved
            and quantities_match
            and prices_match
            and total_matches
            and approval_satisfied
        )

        result = MatchResultCreate(
            invoice_id=inp.invoice_id,
            vendor_known=vendor_known,
            po_resolved=po_resolved,
            contract_resolved=contract_resolved,
            quantities_match=quantities_match,
            prices_match=prices_match,
            total_matches=total_matches,
            approval_satisfied=approval_satisfied,
            overall_passed=overall_passed,
            total_variance_abs=total_variance_abs,
            total_variance_pct=total_variance_pct,
            line_item_details=line_details,
        )

        logger.info(
            "Matching completed",
            extra={
                "invoice_id": inp.invoice_id,
                "overall_passed": overall_passed,
                "vendor_known": vendor_known,
                "po_resolved": po_resolved,
                "contract_resolved": contract_resolved,
                "quantities_match": quantities_match,
                "prices_match": prices_match,
                "total_matches": total_matches,
                "approval_satisfied": approval_satisfied,
            },
        )
        return result


# ---------------------------------------------------------------------------
# Pure sub-check functions
# ---------------------------------------------------------------------------


def _check_vendor(vendor: VendorCreate | None) -> bool:
    """
    FR-2.5: vendor must exist in the approved vendor master and be active.

    Returns False if vendor is None (not found) or not active.
    """
    if vendor is None:
        return False
    return vendor.is_active


def _check_line_items(
    invoice: InvoiceCreate,
    po: PurchaseOrderCreate | None,
    contract: ContractCreate | None,
    tolerance_pct: Decimal,
) -> tuple[list[LineItemMatchDetail], bool, bool]:
    """
    FR-2.2: compare each invoice line against the PO (qty) and contract (price).

    Matching strategy:
      - Quantity  compared against PO line items, matched by line_number.
      - Unit price compared against contract line items, matched by line_number.
      - If PO or contract is None, the respective check is treated as failed
        for every line (po_resolved / contract_resolved handle the coarse gate;
        this function records per-line detail when data is available).

    Returns:
        (line_details, quantities_match, prices_match)
        quantities_match = True only if ALL lines pass qty check.
        prices_match     = True only if ALL lines pass price check.
    """
    # Build lookup maps keyed by line_number.
    po_lines = {li.line_number: li for li in po.line_items} if po else {}
    contract_lines = (
        {li.line_number: li for li in contract.line_items} if contract else {}
    )

    details: list[LineItemMatchDetail] = []
    all_qty_ok = True
    all_price_ok = True

    for inv_line in invoice.line_items:
        ln = inv_line.line_number

        # --- Qty check (against PO) ---
        if ln in po_lines:
            ordered_qty = po_lines[ln].qty
            qty_var_abs, qty_var_pct = compute_variance(inv_line.qty, ordered_qty)
            qty_ok = within_tolerance(qty_var_pct, tolerance_pct)
        else:
            # PO line not found for this line number — treat as mismatch.
            ordered_qty = Decimal("0")
            qty_var_abs = inv_line.qty
            qty_var_pct = Decimal("1")
            qty_ok = False

        # --- Price check (against contract) ---
        if ln in contract_lines:
            contract_price = contract_lines[ln].unit_price
            price_var_abs, price_var_pct = compute_variance(
                inv_line.unit_price, contract_price
            )
            price_ok = within_tolerance(price_var_pct, tolerance_pct)
        else:
            # Contract line not found — treat as mismatch.
            contract_price = Decimal("0")
            price_var_abs = inv_line.unit_price
            price_var_pct = Decimal("1")
            price_ok = False

        details.append(
            LineItemMatchDetail(
                line_number=ln,
                billed_qty=inv_line.qty,
                ordered_qty=ordered_qty,
                qty_variance_abs=qty_var_abs,
                qty_match=qty_ok,
                billed_unit_price=inv_line.unit_price,
                contract_unit_price=contract_price,
                price_variance_abs=price_var_abs,
                price_variance_pct=price_var_pct,
                price_match=price_ok,
            )
        )

        if not qty_ok:
            all_qty_ok = False
        if not price_ok:
            all_price_ok = False

    # If there are no PO lines at all, qty can't pass.
    if not po_lines:
        all_qty_ok = False
    # If there are no contract lines at all, price can't pass.
    if not contract_lines:
        all_price_ok = False

    return details, all_qty_ok, all_price_ok


def _check_total(
    invoice: InvoiceCreate,
    po: PurchaseOrderCreate | None,
    tolerance_pct: Decimal,
) -> tuple[bool, Decimal | None, Decimal | None]:
    """
    FR-2.3: compare invoice grand_total against PO total within tolerance.

    Returns:
        (total_matches, variance_abs, variance_pct)
        If PO is None, returns (False, None, None).
    """
    if po is None:
        return False, None, None

    variance_abs, variance_pct = compute_variance(invoice.grand_total, po.po_total)
    matches = within_tolerance(variance_pct, tolerance_pct)
    return matches, variance_abs, variance_pct


def _check_approval(
    invoice: InvoiceCreate,
    po: PurchaseOrderCreate | None,
    approval_on_file: bool,
) -> bool:
    """
    FR-2.4: if the invoice grand_total is at or above the approval threshold,
    a valid approval must be on file.

    Threshold source (in priority order):
      1. PO's approval_threshold field (denormalised per-PO value).
      2. If PO is None, assume threshold is exceeded (conservative: unknown PO
         means we cannot confirm approval is not required).

    Returns True if:
      - grand_total < approval_threshold  (approval not required), OR
      - grand_total >= threshold AND approval_on_file is True.
    Returns False if:
      - grand_total >= threshold AND approval_on_file is False.
    """
    if po is None:
        # Cannot determine threshold — conservatively fail.
        return approval_on_file

    threshold = po.approval_threshold
    if invoice.grand_total < threshold:
        return True  # Under threshold — no approval needed.
    return approval_on_file  # At or over threshold — approval required.
