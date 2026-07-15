"""
matching/tolerance.py — Percentage-variance helpers for the matching engine.

spec.md Phase 3 / requirements.md FR-2.2, FR-2.3:
  Variance is expressed as a percentage of the reference (PO/contract) value.
  The configurable threshold is MATCH_TOLERANCE_PERCENT (default 0.0, i.e.
  exact match required).

All arithmetic uses Decimal to avoid floating-point drift on financial values.
These are pure functions — no I/O, no LLM calls, no side effects.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

# Precision for internal percentage calculations.
_PCT_PRECISION = Decimal("0.000001")


def compute_variance(
    billed: Decimal,
    reference: Decimal,
) -> tuple[Decimal, Decimal]:
    """
    Compute absolute and percentage variance between a billed value and a
    reference (PO / contract) value.

    Args:
        billed:    The value as stated on the invoice.
        reference: The value from the PO or contract (the "expected" value).

    Returns:
        (variance_abs, variance_pct) where:
          variance_abs = billed − reference
          variance_pct = variance_abs / reference  (0.0 if reference == 0)

    Both values are signed — positive means the invoice is higher than expected.
    """
    variance_abs = billed - reference
    if reference == Decimal(0):
        # Avoid division by zero; treat as 100% variance if reference is 0
        # and billed is non-zero, or 0% if both are 0.
        variance_pct = Decimal("1") if billed != Decimal(0) else Decimal("0")
    else:
        variance_pct = (variance_abs / reference).quantize(
            _PCT_PRECISION, rounding=ROUND_HALF_UP
        )
    return variance_abs, variance_pct


def within_tolerance(variance_pct: Decimal, tolerance_pct: Decimal) -> bool:
    """
    Return True if the absolute percentage variance is within the configured
    tolerance threshold.

    Args:
        variance_pct:  Signed percentage variance (from compute_variance).
        tolerance_pct: Maximum allowed absolute percentage variance
                       (from MATCH_TOLERANCE_PERCENT, expressed as a plain
                       percentage, e.g. 2.0 means ±2%).

    Note: tolerance_pct is in percentage-point units (e.g. 2.0 = 2%), while
    variance_pct from compute_variance is a fraction (e.g. 0.02 = 2%).
    The conversion is applied here so callers use the settings value directly.
    """
    # Convert tolerance from percentage points to a fraction for comparison.
    tolerance_fraction = tolerance_pct / Decimal("100")
    return abs(variance_pct) <= tolerance_fraction
