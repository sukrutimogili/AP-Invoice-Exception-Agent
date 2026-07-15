"""
ui/pages/discount_optimization.py — Discount Optimization page (Phase 7).

Lets the user explore the discount recommendation engine interactively:
  - Enter a discount term string (e.g. "2/10 net 30") — parsed by discount.parser
  - Set the invoice amount and hurdle rate
  - Runs discount.calculator.evaluate_discount() directly
  - Shows the annualized return formula broken down step by step
  - Shows the recommendation badge with a plain-English explanation

All arithmetic is delegated to discount/calculator.py (pure deterministic math,
zero LLM calls per spec.md §5).  Zero business logic lives in this file.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import streamlit as st

from discount.calculator import (
    DiscountGateError,
    annualized_return_formula,
    compute_discount_amount,
    evaluate_discount,
)
from discount.parser import parse_discount_term
from models.contract import DiscountTermSchema
from models.enums import DiscountRecommendation
from ui.components.badges import discount_badge


# ---------------------------------------------------------------------------
# Plain-English explanation builder
# ---------------------------------------------------------------------------

def _plain_english(
    recommendation: DiscountRecommendation,
    discount_pct: Decimal | None,
    net_days: int | None,
    discount_days: int | None,
    annualized_return: Decimal | None,
    hurdle_rate: Decimal,
    discount_amount: Decimal | None,
    invoice_amount: Decimal,
    window_missed: bool,
) -> str:
    if recommendation == DiscountRecommendation.NO_DISCOUNT:
        return (
            "This contract does not offer an early-payment discount, "
            "so payment is scheduled at the standard due date."
        )

    if window_missed or recommendation == DiscountRecommendation.WINDOW_MISSED:
        return (
            f"The discount window for this invoice has already passed.  "
            f"Payment is scheduled at the standard net-{net_days} terms.  "
            "This is recorded for visibility — no exception is raised (FR-7.5)."
        )

    if recommendation == DiscountRecommendation.TAKE_DISCOUNT:
        pct_display = float(discount_pct or 0) * 100
        ann_pct = float(annualized_return or 0) * 100
        hurdle_pct = float(hurdle_rate) * 100
        return (
            f"**Recommendation: Take the discount.**  \n\n"
            f"Paying within **{discount_days} days** (instead of {net_days}) "
            f"earns a **{pct_display:.2f}%** discount, saving "
            f"**${discount_amount or Decimal(0):.2f}** on a "
            f"${invoice_amount:.2f} invoice.  \n\n"
            f"The annualized effective return of taking this discount is "
            f"**{ann_pct:.2f}%**, which is above the company's hurdle rate of "
            f"**{hurdle_pct:.2f}%** — meaning it costs less to pay early than "
            "to keep that cash in other uses."
        )

    if recommendation == DiscountRecommendation.HOLD_TO_NET:
        pct_display = float(discount_pct or 0) * 100
        ann_pct = float(annualized_return or 0) * 100
        hurdle_pct = float(hurdle_rate) * 100
        return (
            f"**Recommendation: Hold to net terms.**  \n\n"
            f"Paying within **{discount_days} days** would earn a "
            f"**{pct_display:.2f}%** discount, but the annualized effective "
            f"return of doing so is only **{ann_pct:.2f}%** — below the "
            f"company's hurdle rate of **{hurdle_pct:.2f}%**.  \n\n"
            "It is more efficient to keep the cash for other uses and pay at "
            f"the standard net-{net_days} due date."
        )

    return ""


# ---------------------------------------------------------------------------
# Formula step-by-step display
# ---------------------------------------------------------------------------

def _render_formula_breakdown(
    discount_pct: Decimal,
    net_days: int,
    discount_days: int,
    annualized_return: Decimal,
    hurdle_rate: Decimal,
) -> None:
    """Show the FR-7.2 formula with substituted values."""
    d = discount_pct
    days_spread = net_days - discount_days
    discount_factor = d / (Decimal("1") - d)
    annualization = Decimal("365") / Decimal(str(days_spread))

    st.markdown("### Formula Breakdown (FR-7.2)")
    st.latex(
        r"\text{Annualized Return} = \frac{d}{1-d} \times \frac{365}{\text{net} - \text{disc}}"
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("d (discount %)", f"{float(d)*100:.4f}%")
    col2.metric("Discount factor  d/(1−d)", f"{float(discount_factor):.6f}")
    col3.metric("Days spread (net−disc)", str(days_spread))
    col4.metric("Annualization × (365/spread)", f"{float(annualization):.4f}×")

    st.markdown(
        f"**Result:** `{float(discount_factor):.6f}` × `{float(annualization):.4f}` "
        f"= **`{float(annualized_return):.6f}`** "
        f"({float(annualized_return)*100:.2f}% annualized return)"
    )

    hurdle_pct = float(hurdle_rate) * 100
    ann_pct = float(annualized_return) * 100
    comparison = "≥" if annualized_return >= hurdle_rate else "<"
    colour = "green" if annualized_return >= hurdle_rate else "red"
    st.markdown(
        f":{colour}[**{ann_pct:.2f}%** annualized return "
        f"{comparison} **{hurdle_pct:.2f}%** hurdle rate]"
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def render() -> None:
    st.title("💰 Discount Optimization")
    st.markdown(
        "Explore the early-payment discount calculator (FR-7).  "
        "All arithmetic is deterministic Python — no LLM involved (spec.md §5)."
    )

    # -----------------------------------------------------------------------
    # Recent discount events from audit trail
    # -----------------------------------------------------------------------
    import audit.writer as audit_writer
    import json as _json
    from models.enums import AuditEventType

    all_events = audit_writer.get_all_events()
    discount_events = [
        e for e in all_events
        if e.get("event_type") == AuditEventType.DISCOUNT_EVALUATED.value
    ]

    if discount_events:
        st.subheader(f"Recent Discount Evaluations ({len(discount_events)} total)")
        for ev in reversed(discount_events[-5:]):  # show last 5
            try:
                payload = _json.loads(ev.get("payload_json") or "{}")
            except Exception:
                payload = {}

            rec = payload.get("recommendation", "UNKNOWN")
            col_a, col_b = st.columns([2, 5])
            with col_a:
                st.markdown(discount_badge(rec), unsafe_allow_html=True)
            with col_b:
                ann = payload.get("annualized_return")
                hurdle = payload.get("hurdle_rate")
                inv_num = ev.get("invoice_number", "—")
                pct = payload.get("discount_pct")
                details = f"Invoice `{inv_num}`"
                if ann:
                    details += f"  ·  Return: **{float(ann)*100:.2f}%**"
                if hurdle:
                    details += f"  ·  Hurdle: **{float(hurdle)*100:.2f}%**"
                if pct:
                    details += f"  ·  Discount: **{float(pct)*100:.2f}%**"
                st.markdown(details)

        st.divider()

    # -----------------------------------------------------------------------
    # Interactive calculator
    # -----------------------------------------------------------------------
    st.subheader("🧮 Interactive Calculator")

    with st.form("discount_calc_form"):
        col1, col2 = st.columns(2)

        with col1:
            discount_term_raw = st.text_input(
                "Discount term string",
                value="2/10 net 30",
                help='Standard format: "X/Y net Z" — e.g. "2/10 net 30" = 2% if paid within 10 days.',
            )
            invoice_amount_str = st.text_input(
                "Invoice amount ($)",
                value="10000.00",
            )
            invoice_date = st.date_input(
                "Invoice date",
                value=date.today() - timedelta(days=2),
                help="Used to compute the discount deadline.",
            )

        with col2:
            hurdle_rate_pct = st.number_input(
                "Hurdle rate (%)",
                min_value=0.1,
                max_value=99.9,
                value=10.0,
                step=0.5,
                format="%.1f",
                help="Your cost of capital. Default: 10% (DISCOUNT_HURDLE_RATE_DEFAULT).",
            )
            processing_date = st.date_input(
                "Processing date",
                value=date.today(),
                help="The date the invoice is being evaluated (today by default).",
            )

        calc_submitted = st.form_submit_button("📊 Calculate", type="primary")

    if calc_submitted:
        # 1. Parse discount term
        parsed_term: DiscountTermSchema | None = None
        if discount_term_raw.strip():
            try:
                parsed_term = parse_discount_term(discount_term_raw.strip())
            except Exception as e:
                st.error(f"Could not parse discount term: {e}")
                return

        if parsed_term is None and discount_term_raw.strip():
            st.warning(
                f"Could not parse `{discount_term_raw}` as a standard discount term.  "
                "Expected format: `X/Y net Z` (e.g. `2/10 net 30`)."
            )
            return

        # 2. Parse invoice amount
        try:
            invoice_amount = Decimal(invoice_amount_str.strip())
            if invoice_amount <= 0:
                raise ValueError("Invoice amount must be positive.")
        except (InvalidOperation, ValueError) as e:
            st.error(f"Invalid invoice amount: {e}")
            return

        # 3. Evaluate discount
        hurdle_rate = Decimal(str(hurdle_rate_pct / 100))

        try:
            rec = evaluate_discount(
                invoice_id="ui-calculator",
                invoice_amount=invoice_amount,
                invoice_date=invoice_date,
                discount_term=parsed_term,
                hurdle_rate=hurdle_rate,
                processing_date=processing_date,
                is_stp_eligible=True,
            )
        except DiscountGateError as e:
            st.error(f"Gate error: {e}")
            return
        except Exception as e:
            st.error(f"Calculation error: {e}")
            return

        # -----------------------------------------------------------------------
        # Render results
        # -----------------------------------------------------------------------
        st.divider()

        # Recommendation badge
        st.markdown("### Recommendation")
        st.markdown(discount_badge(rec.recommendation.value), unsafe_allow_html=True)

        # Parsed term display
        if parsed_term:
            st.markdown(
                f"**Parsed term:** `{parsed_term.discount_term_raw}` → "
                f"{float(parsed_term.discount_pct)*100:.3f}% discount "
                f"if paid within **{parsed_term.discount_days} days** "
                f"(net {parsed_term.net_days})"
            )

        # Key metrics
        cols = st.columns(4)
        cols[0].metric(
            "Discount Amount",
            f"${rec.discount_amount:.2f}" if rec.discount_amount else "—",
        )
        cols[1].metric(
            "Annualized Return",
            f"{float(rec.annualized_return)*100:.2f}%" if rec.annualized_return else "—",
        )
        cols[2].metric("Hurdle Rate", f"{float(rec.hurdle_rate)*100:.2f}%")
        cols[3].metric(
            "Discount Deadline",
            str(rec.discount_date) if rec.discount_date else "—",
        )

        # Formula breakdown (only when annualized return was computed)
        if rec.annualized_return is not None and parsed_term is not None:
            with st.expander("📐 Formula Breakdown", expanded=True):
                _render_formula_breakdown(
                    discount_pct=parsed_term.discount_pct,
                    net_days=parsed_term.net_days,
                    discount_days=parsed_term.discount_days,
                    annualized_return=rec.annualized_return,
                    hurdle_rate=hurdle_rate,
                )

        # Plain-English explanation
        explanation = _plain_english(
            recommendation=rec.recommendation,
            discount_pct=rec.discount_pct,
            net_days=parsed_term.net_days if parsed_term else None,
            discount_days=parsed_term.discount_days if parsed_term else None,
            annualized_return=rec.annualized_return,
            hurdle_rate=hurdle_rate,
            discount_amount=rec.discount_amount,
            invoice_amount=invoice_amount,
            window_missed=rec.window_missed,
        )
        if explanation:
            st.info(explanation)
