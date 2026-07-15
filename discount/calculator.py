"""
discount/calculator.py — Pure deterministic discount-optimization calculator.

spec.md Phase 7 / requirements.md FR-7.

Implements the annualized-return formula from FR-7.2 exactly:

    annualized_return = (d / (1 - d)) × (365 / (net_days - discount_days))

where d = discount_pct as a fraction (e.g. 0.02 for 2%).

Design rules (non-negotiable, per spec.md §5):
  - ZERO LLM calls in this module — pure arithmetic only.
  - All inputs must be validated Pydantic types; no bare dicts cross the boundary.
  - FR-7.4 gate: the public entry-point refuses to compute if the invoice has
    not passed matching (i.e. is an exception or rejected invoice).
  - FR-7.5: if the discount window has lapsed by processing_date, record
    DISCOUNT_WINDOW_MISSED and do NOT raise an exception — this is visibility.
  - FR-7.3: returns a DiscountRecommendationCreate that the caller must persist
    and audit; this module does not write to the audit log itself.

Public API
----------
evaluate_discount(
    invoice_id,
    invoice_amount,
    invoice_date,
    discount_term,      # DiscountTermSchema | None
    hurdle_rate,        # Decimal — cost-of-capital threshold
    processing_date,    # date — today; used to detect window lapse (FR-7.5)
    is_stp_eligible,    # bool  — FR-7.4 gate
) -> DiscountRecommendationCreate

annualized_return_formula(discount_pct, net_days, discount_days) -> Decimal
    The raw formula, exposed for unit-testing.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from models.contract import DiscountTermSchema
from models.discount_recommendation import DiscountRecommendationCreate
from models.enums import DiscountRecommendation

logger = logging.getLogger(__name__)

# Precision used throughout all intermediate calculations.
# 10 decimal places is well beyond the precision of any real discount term.
_PRECISION = Decimal("0.0000000001")

# Display precision for annualized_return stored in the DB / audit.
_RETURN_PRECISION = Decimal("0.000001")  # 6 d.p., matches the ORM column Numeric(10,6)

# Discount amount precision (monetary).
_MONEY_PRECISION = Decimal("0.01")


# ---------------------------------------------------------------------------
# Core formula (FR-7.2) — pure arithmetic, no I/O
# ---------------------------------------------------------------------------


def annualized_return_formula(
    discount_pct: Decimal,
    net_days: int,
    discount_days: int,
) -> Decimal:
    """
    Compute the annualized effective return of taking a discount.

    FR-7.2 formula:
        (discount_pct / (1 - discount_pct)) × (365 / (net_days - discount_days))

    Args:
        discount_pct:   Discount fraction (0 < d < 1). E.g. 0.02 for 2%.
        net_days:       Standard net payment days (e.g. 30 for "net 30").
        discount_days:  Discount window days (e.g. 10 for "2/10 net 30").

    Returns:
        Annualized return as a Decimal, rounded to 6 decimal places.

    Raises:
        ValueError: if discount_pct is not in (0, 1) or net_days <= discount_days.
    """
    if not (Decimal("0") < discount_pct < Decimal("1")):
        raise ValueError(
            f"discount_pct must be between 0 and 1 exclusive, got {discount_pct}"
        )
    if net_days <= discount_days:
        raise ValueError(
            f"net_days ({net_days}) must be greater than discount_days ({discount_days})"
        )

    d = discount_pct
    days_spread = Decimal(str(net_days - discount_days))

    # (d / (1 - d)) — discount factor
    discount_factor = d / (Decimal("1") - d)

    # × (365 / days_spread) — annualization factor
    annualization = Decimal("365") / days_spread

    result = discount_factor * annualization
    return result.quantize(_RETURN_PRECISION, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Discount amount helper
# ---------------------------------------------------------------------------


def compute_discount_amount(invoice_amount: Decimal, discount_pct: Decimal) -> Decimal:
    """
    Compute the absolute discount amount for an invoice.

    discount_amount = invoice_amount × discount_pct

    Args:
        invoice_amount: Invoice grand total (> 0).
        discount_pct:   Discount fraction (0 < d < 1).

    Returns:
        Discount amount rounded to 2 decimal places (monetary).
    """
    return (invoice_amount * discount_pct).quantize(_MONEY_PRECISION, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# FR-7.4 gate
# ---------------------------------------------------------------------------


class DiscountGateError(Exception):
    """
    Raised when discount evaluation is attempted on a non-STP invoice.

    FR-7.4: discount logic only runs on invoices that have already passed
    matching (i.e. is_stp_eligible is True).  Callers that receive this
    exception have a bug — they called evaluate_discount on an exception or
    rejected invoice.
    """


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------


def evaluate_discount(
    *,
    invoice_id: str,
    invoice_amount: Decimal,
    invoice_date: date,
    discount_term: DiscountTermSchema | None,
    hurdle_rate: Decimal,
    processing_date: date,
    is_stp_eligible: bool,
) -> DiscountRecommendationCreate:
    """
    Evaluate whether an early-payment discount should be taken.

    This is the sole public entry-point for all discount logic.  It enforces
    the FR-7.4 gate, handles the FR-7.5 window-missed case, and delegates the
    arithmetic to ``annualized_return_formula``.

    Args:
        invoice_id:       Stable ID for the invoice (for the recommendation record).
        invoice_amount:   Invoice grand total (Decimal, > 0).
        invoice_date:     Date the invoice was issued (used to compute discount deadline).
        discount_term:    Parsed DiscountTermSchema from the contract, or None if the
                          contract has no discount clause.
        hurdle_rate:      Configured cost-of-capital threshold (e.g. Decimal("0.10")).
                          Sourced from settings.discount_hurdle_rate_default.
        processing_date:  The date on which the invoice is being evaluated (today).
                          Used to detect whether the discount window has already lapsed.
        is_stp_eligible:  Must be True for discount logic to run (FR-7.4).
                          Pass the value of MatchResult.overall_passed.

    Returns:
        A fully-populated DiscountRecommendationCreate ready for persistence and audit.

    Raises:
        DiscountGateError: if is_stp_eligible is False (FR-7.4 — discount logic
                           must never run on exception or rejected invoices).
    """
    # -----------------------------------------------------------------------
    # FR-7.4 gate — must be checked unconditionally before any other logic
    # -----------------------------------------------------------------------
    if not is_stp_eligible:
        raise DiscountGateError(
            f"FR-7.4 violation: evaluate_discount called for invoice_id={invoice_id!r} "
            "but is_stp_eligible=False. Discount logic must only run on STP-eligible invoices. "
            "This is a caller bug — check the routing decision before calling evaluate_discount."
        )

    logger.info(
        "Evaluating discount",
        extra={
            "invoice_id": invoice_id,
            "has_discount_term": discount_term is not None,
            "processing_date": str(processing_date),
        },
    )

    # -----------------------------------------------------------------------
    # No discount term on the contract → NO_DISCOUNT
    # -----------------------------------------------------------------------
    if discount_term is None:
        logger.info("No discount term on contract", extra={"invoice_id": invoice_id})
        return DiscountRecommendationCreate(
            invoice_id=invoice_id,
            invoice_amount=invoice_amount,
            discount_pct=None,
            discount_days=None,
            net_days=None,
            discount_amount=None,
            annualized_return=None,
            hurdle_rate=hurdle_rate,
            recommendation=DiscountRecommendation.NO_DISCOUNT,
            discount_date=None,
            window_missed=False,
        )

    # -----------------------------------------------------------------------
    # Compute the discount deadline
    # -----------------------------------------------------------------------
    discount_date: date = invoice_date + timedelta(days=discount_term.discount_days)

    # -----------------------------------------------------------------------
    # FR-7.5: discount window already lapsed?
    # -----------------------------------------------------------------------
    if processing_date > discount_date:
        logger.info(
            "Discount window missed",
            extra={
                "invoice_id": invoice_id,
                "discount_date": str(discount_date),
                "processing_date": str(processing_date),
            },
        )
        return DiscountRecommendationCreate(
            invoice_id=invoice_id,
            invoice_amount=invoice_amount,
            discount_pct=discount_term.discount_pct,
            discount_days=discount_term.discount_days,
            net_days=discount_term.net_days,
            discount_amount=compute_discount_amount(invoice_amount, discount_term.discount_pct),
            annualized_return=None,  # not computed when window missed
            hurdle_rate=hurdle_rate,
            recommendation=DiscountRecommendation.WINDOW_MISSED,
            discount_date=discount_date,
            window_missed=True,
        )

    # -----------------------------------------------------------------------
    # FR-7.2: compute annualized return and compare to hurdle rate
    # -----------------------------------------------------------------------
    ann_return = annualized_return_formula(
        discount_pct=discount_term.discount_pct,
        net_days=discount_term.net_days,
        discount_days=discount_term.discount_days,
    )
    discount_amount = compute_discount_amount(invoice_amount, discount_term.discount_pct)

    # FR-7.2: TAKE if annualized_return >= hurdle_rate, else HOLD
    if ann_return >= hurdle_rate:
        recommendation = DiscountRecommendation.TAKE_DISCOUNT
    else:
        recommendation = DiscountRecommendation.HOLD_TO_NET

    logger.info(
        "Discount evaluated",
        extra={
            "invoice_id": invoice_id,
            "discount_pct": str(discount_term.discount_pct),
            "annualized_return": str(ann_return),
            "hurdle_rate": str(hurdle_rate),
            "recommendation": recommendation.value,
        },
    )

    return DiscountRecommendationCreate(
        invoice_id=invoice_id,
        invoice_amount=invoice_amount,
        discount_pct=discount_term.discount_pct,
        discount_days=discount_term.discount_days,
        net_days=discount_term.net_days,
        discount_amount=discount_amount,
        annualized_return=ann_return,
        hurdle_rate=hurdle_rate,
        recommendation=recommendation,
        discount_date=discount_date,
        window_missed=False,
    )
