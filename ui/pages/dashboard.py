"""
ui/pages/dashboard.py — Operations Dashboard.

Live summary of the invoice processing pipeline pulled directly from the
in-process stores and audit log.  No mock numbers, no placeholders.

Data sources:
  audit.writer.get_all_events()               — STP rate, recent activity,
                                                vendor-auto-creation count
  services.exception_store.list_exceptions()  — open exception count and
                                                reason-code breakdown
  services.payment_store.list_payment_schedules() — discount capture metrics

Layout:
  1. KPI cards (st.metric row)
  2. STP vs Exception breakdown bar chart
  3. Open exceptions by reason code (bar chart)
  4. Discount capture summary (metrics + table)
  5. Vendor auto-creation alert
  6. Recent activity feed (last 15 audit events)
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
import streamlit as st

import audit.writer as audit_writer
import services.exception_store as exception_store
import services.payment_store as payment_store
from models.enums import AuditEventType, ExceptionReasonCode
from ui.components.badges import AUDIT_EVENT_COLOURS, render_badge
from ui.components.theme import inject_theme

# ---------------------------------------------------------------------------
# Event-type text labels  (colours sourced centrally from badges.AUDIT_EVENT_COLOURS)
# No emoji — labels use short descriptive text rendered inside badge pills.
# ---------------------------------------------------------------------------

_EVENT_LABELS: dict[str, str] = {
    AuditEventType.INVOICE_RECEIVED.value:        "Received",
    AuditEventType.EXTRACTION_SUCCEEDED.value:    "Extracted",
    AuditEventType.EXTRACTION_FAILED.value:       "Extraction Failed",
    AuditEventType.MATCHING_COMPLETED.value:      "Matched",
    AuditEventType.STP_APPROVED.value:            "STP Approved",
    AuditEventType.EXCEPTION_RAISED.value:        "Exception",
    AuditEventType.HUMAN_OVERRIDE_APPROVED.value: "Human Approved",
    AuditEventType.HUMAN_REJECTED.value:          "Rejected",
    AuditEventType.PAYMENT_SCHEDULED.value:       "Payment Scheduled",
    AuditEventType.DISCOUNT_EVALUATED.value:      "Discount Evaluated",
    AuditEventType.DOCUMENT_CONFLICT_DETECTED.value: "Doc Conflict",
    AuditEventType.VENDOR_AUTO_CREATED.value:     "Vendor Auto-Created",
}

_EVENT_COLOURS = AUDIT_EVENT_COLOURS

# Risk level → badge colour
_RISK_COLOURS = {
    "high":   "#C0392B",   # red
    "medium": "#B45309",   # amber
    "low":    "#2563A8",   # blue
}


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_data() -> tuple[
    list[dict[str, Any]],           # all audit events
    list,                            # ExceptionDecision list
    list,                            # PaymentScheduleCreate list
]:
    """Load all data sources in one place so the page renders consistently."""
    return (
        audit_writer.get_all_events(),
        exception_store.list_exceptions(),
        payment_store.list_payment_schedules(),
    )


def _stp_exception_counts(events: list[dict[str, Any]]) -> tuple[int, int]:
    """Return (stp_count, exception_count) derived from audit events."""
    counts = Counter(e.get("event_type", "") for e in events)
    return (
        counts.get(AuditEventType.STP_APPROVED.value, 0),
        counts.get(AuditEventType.EXCEPTION_RAISED.value, 0),
    )


def _stp_rate(stp: int, exc: int) -> float:
    """Return STP rate as a 0–100 float, or 0.0 if no invoices processed."""
    total = stp + exc
    return (stp / total * 100) if total > 0 else 0.0


def _open_exception_breakdown(decisions: list) -> dict[str, int]:
    """
    Return a {reason_code_label: count} dict for open exceptions only.
    Uses ExceptionReasonCode enum values as labels.
    """
    counts: dict[str, int] = {}
    for decision in decisions:
        record = decision.exception_record
        # Skip resolved exceptions (human acted on them)
        from models.enums import ExceptionStatus
        if record.status != ExceptionStatus.OPEN:
            continue
        for reason in record.reasons:
            label = reason.reason_code.value
            counts[label] = counts.get(label, 0) + 1
    return counts


def _discount_metrics(schedules: list) -> tuple[Decimal, Decimal, int, int]:
    """
    Derive discount capture metrics from payment schedules.

    Returns:
        captured_total    — sum of discount_amount where discount_taken=True
        available_skipped — sum of discount_amount where discount_taken=False
                            and discount_amount is not None (discount existed
                            but was not taken)
        taken_count       — number of schedules where discount_taken=True
        skipped_count     — number where discount existed but was skipped
    """
    captured = Decimal("0")
    skipped = Decimal("0")
    taken_count = 0
    skipped_count = 0

    for sched in schedules:
        da = sched.discount_amount  # Decimal | None
        if sched.discount_taken and da is not None:
            captured += da
            taken_count += 1
        elif not sched.discount_taken and da is not None:
            skipped += da
            skipped_count += 1

    return captured, skipped, taken_count, skipped_count


def _vendor_auto_created_count(events: list[dict[str, Any]]) -> int:
    return sum(
        1 for e in events
        if e.get("event_type") == AuditEventType.VENDOR_AUTO_CREATED.value
    )


def _recent_events(events: list[dict[str, Any]], n: int = 15) -> list[dict[str, Any]]:
    """Return the n most recent events, newest first."""
    return list(reversed(events))[:n]


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _render_kpi_cards(
    stp: int,
    exc: int,
    open_exc: int,
    captured: Decimal,
    vendor_auto: int,
) -> None:
    """Row of five KPI metric cards."""
    rate = _stp_rate(stp, exc)
    total_processed = stp + exc

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(
        "STP Rate",
        f"{rate:.1f}%",
        help=f"{stp} STP out of {total_processed} processed invoices",
    )
    c2.metric(
        "STP / Exception",
        f"{stp} / {exc}",
        help="Count of STP_APPROVED vs EXCEPTION_RAISED audit events",
    )
    c3.metric(
        "Open Exceptions",
        open_exc,
        help="Unresolved items in the exception queue",
    )
    c4.metric(
        "Discount Captured",
        f"${captured:,.2f}",
        help="Total early-payment discount amount taken on STP invoices",
    )
    c5.metric(
        "Vendor Auto-Creates",
        vendor_auto,
        delta=f"+{vendor_auto}" if vendor_auto > 0 else None,
        delta_color="inverse" if vendor_auto > 0 else "off",
        help="VENDOR_AUTO_CREATED events — unattended onboarding, worth reviewing",
    )


def _render_stp_chart(stp: int, exc: int) -> None:
    """Bar chart: STP vs Exception routing split."""
    st.subheader("Routing Outcome Split")
    total = stp + exc
    if total == 0:
        st.info("No invoices processed yet.")
        return

    df = pd.DataFrame(
        {"Count": [stp, exc]},
        index=["STP Approved", "Exception Raised"],
    )
    st.bar_chart(df, color="#1A6B2A" if stp >= exc else "#C0392B")
    st.caption(
        f"STP rate: **{_stp_rate(stp, exc):.1f}%** "
        f"({stp} straight-through out of {total} total)"
    )


def _render_open_exceptions(decisions: list) -> None:
    """Open exception count with reason-code bar chart."""
    st.subheader("Open Exceptions by Reason Code")

    breakdown = _open_exception_breakdown(decisions)
    open_count = sum(breakdown.values())

    if open_count == 0:
        st.success("No open exceptions — queue is clear.")
        return

    st.metric("Total Open", open_count)

    # Build a dataframe with all known reason codes so missing ones show 0.
    all_codes = [rc.value for rc in ExceptionReasonCode]
    counts = [breakdown.get(code, 0) for code in all_codes]

    df = pd.DataFrame({"Open Count": counts}, index=all_codes)
    # Drop zero rows to keep the chart readable.
    df = df[df["Open Count"] > 0]

    st.bar_chart(df)

    # Table with human-readable labels alongside counts.
    rows = [
        {"Reason Code": code, "Open Count": cnt}
        for code, cnt in sorted(breakdown.items(), key=lambda x: -x[1])
    ]
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )


def _render_discount_capture(schedules: list) -> None:
    """Discount capture summary: metrics + per-invoice table."""
    st.subheader("Discount Capture")

    captured, skipped, taken_count, skipped_count = _discount_metrics(schedules)
    total_with_discount = taken_count + skipped_count

    if total_with_discount == 0:
        st.info(
            "No discount opportunities recorded yet.  "
            "Discount data appears once contracts with discount terms are processed."
        )
        return

    capture_rate = (taken_count / total_with_discount * 100) if total_with_discount > 0 else 0.0

    c1, c2, c3 = st.columns(3)
    c1.metric("Captured", f"${captured:,.2f}", help=f"{taken_count} invoice(s) took early payment")
    c2.metric("Available but Skipped", f"${skipped:,.2f}", help=f"{skipped_count} invoice(s) held to net terms")
    c3.metric("Capture Rate", f"{capture_rate:.1f}%", help="Discounts taken / total discount opportunities")

    # Per-invoice breakdown table.
    rows = []
    for sched in schedules:
        if sched.discount_amount is None:
            continue  # no discount term on this invoice
        rows.append({
            "Invoice ID": sched.invoice_id[:8] + "…",
            "Scheduled Date": str(sched.scheduled_date),
            "Amount": f"${sched.amount:,.2f}",
            "Discount": f"${sched.discount_amount:,.2f}",
            "Taken": "Yes" if sched.discount_taken else "No",
        })

    if rows:
        with st.expander("Per-Invoice Discount Detail", expanded=False):
            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
            )


def _render_vendor_auto_creates(events: list[dict[str, Any]]) -> None:
    """Alert card for vendor auto-creation events."""
    vc_events = [
        e for e in events
        if e.get("event_type") == AuditEventType.VENDOR_AUTO_CREATED.value
    ]
    count = len(vc_events)

    st.subheader("Vendor Auto-Creation")

    if count == 0:
        st.success("No unattended vendor onboardings recorded this session.")
        return

    st.warning(
        f"**{count} vendor(s) were auto-created** from uploaded PO or contract "
        f"documents during this session.  These vendors were not previously in the "
        f"master list — review them to confirm the details are correct before "
        f"future invoices are processed against them."
    )

    rows = []
    for e in vc_events:
        payload: dict = {}
        raw = e.get("payload_json")
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:
                pass
        rows.append({
            "Vendor Code": payload.get("vendor_code", "—"),
            "Vendor Name": payload.get("vendor_name", "—"),
            "Source Document": payload.get("source_document", "—"),
            "Timestamp": str(e.get("created_at", ""))[:19],
        })

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )


def _render_vendor_risk_review() -> None:
    """
    Vendor Risk Review section — powered by the vendor risk assessment agent.

    Displays flagged vendors with their risk level, reasons, and recommended
    human actions.  No automated action is taken here — this is a review queue.

    The "Run Risk Assessment" button calls assess_vendor_risk() with the live
    DB session and caches results in st.session_state so repeated renders don't
    re-call the LLM.  A human still decides what (if anything) to do.
    """
    st.subheader("Vendor Risk Review")
    st.caption(
        "Agent-generated review queue.  Each flag is derived from exception history, "
        "auto-creation status, and price-variance patterns.  No automated action is "
        "taken — a human reviews and decides."
    )

    # -----------------------------------------------------------------------
    # Show cached results if available
    # -----------------------------------------------------------------------
    cached_flags = st.session_state.get("vendor_risk_flags")

    if cached_flags:
        st.success(
            f"Last assessment flagged **{len(cached_flags)}** vendor(s).  "
            "Click **Run Risk Assessment** to refresh."
        )
        for flag in cached_flags:
            colour = _RISK_COLOURS.get(flag.risk_level, "#4B5563")
            risk_label = f"{flag.risk_level.upper()} RISK — {flag.vendor_code}"
            risk_badge = render_badge(risk_label, colour)
            with st.expander(
                f"{flag.risk_level.upper()} — {flag.vendor_code}",
                expanded=(flag.risk_level == "high"),
            ):
                st.markdown(risk_badge, unsafe_allow_html=True)
                st.markdown("**Reasons:**")
                for reason in flag.reasons:
                    st.markdown(f"- {reason}")
                st.markdown(
                    f"**Recommended action:** {flag.recommended_action}"
                )
    elif cached_flags is not None and len(cached_flags) == 0:
        st.success("No vendor risk flags — all vendors look clean based on current data.")
    else:
        st.info(
            "No assessment run yet.  Click **Run Risk Assessment** to analyse "
            "all vendors against their exception history and auto-creation status."
        )

    # -----------------------------------------------------------------------
    # Run button — triggers the LLM agent, caches results
    # -----------------------------------------------------------------------
    if st.button(
        "Run Risk Assessment",
        key="run_vendor_risk",
        help=(
            "Calls the vendor risk agent for every vendor in the master table.  "
            "LLM call required — results are cached until you click again."
        ),
    ):
        with st.spinner("Running vendor risk assessment — querying LLM for each vendor…"):
            try:
                from agents.vendor_risk_agent import assess_vendor_risk
                from db.session import get_session

                with get_session() as session:
                    flags = assess_vendor_risk(session)

                st.session_state["vendor_risk_flags"] = flags
                st.rerun()
            except Exception as exc:
                st.error(f"Vendor risk assessment failed: {exc}")


def _render_recent_activity(events: list[dict[str, Any]]) -> None:
    """Last 15 audit events in a scannable table with a link to the full log."""
    st.subheader("Recent Activity")
    st.caption(
        "Showing the 15 most recent audit events.  "
        "Use the **Audit Log** page for search, filtering, and full payload detail."
    )

    recent = _recent_events(events, n=15)

    if not recent:
        st.info("No audit events recorded yet.")
        return

    rows = []
    for e in recent:
        event_type = e.get("event_type", "UNKNOWN")
        label = _EVENT_LABELS.get(event_type, event_type.replace("_", " ").title())
        invoice_number = e.get("invoice_number") or "—"
        invoice_id = e.get("invoice_id", "—")
        created_at = str(e.get("created_at", ""))
        rows.append({
            "Time": created_at[:19] if created_at else "—",
            "Event": label,
            "Invoice #": invoice_number,
            "Invoice ID": invoice_id[:8] + "…" if len(invoice_id) > 8 else invoice_id,
        })

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Page entry point
# ---------------------------------------------------------------------------

def render() -> None:
    inject_theme()
    st.title("Operations Dashboard")
    st.markdown(
        "Live pipeline metrics from the current session.  "
        "All numbers are derived directly from the audit log, exception queue, "
        "and payment store — no mock data."
    )

    # -----------------------------------------------------------------------
    # Load all data sources once
    # -----------------------------------------------------------------------
    events, decisions, schedules = _load_data()

    if not events and not decisions and not schedules:
        st.info(
            "No data yet.  Submit invoices on the **Invoice Processing** page "
            "to populate the dashboard."
        )
        return

    # -----------------------------------------------------------------------
    # Derive top-level counts
    # -----------------------------------------------------------------------
    stp, exc = _stp_exception_counts(events)
    open_exc = sum(
        1 for d in decisions
        if d.exception_record.status.value == "OPEN"
    )
    captured, _, _, _ = _discount_metrics(schedules)
    vendor_auto = _vendor_auto_created_count(events)

    # -----------------------------------------------------------------------
    # 1. KPI cards
    # -----------------------------------------------------------------------
    _render_kpi_cards(stp, exc, open_exc, captured, vendor_auto)

    st.divider()

    # -----------------------------------------------------------------------
    # 2. Routing split + open exceptions (side by side)
    # -----------------------------------------------------------------------
    left, right = st.columns(2)
    with left:
        _render_stp_chart(stp, exc)
    with right:
        _render_open_exceptions(decisions)

    st.divider()

    # -----------------------------------------------------------------------
    # 3. Discount capture
    # -----------------------------------------------------------------------
    _render_discount_capture(schedules)

    st.divider()

    # -----------------------------------------------------------------------
    # 4. Vendor auto-creation alert
    # -----------------------------------------------------------------------
    _render_vendor_auto_creates(events)

    st.divider()

    # -----------------------------------------------------------------------
    # 4b. Vendor Risk Review (LLM-powered agent)
    # -----------------------------------------------------------------------
    _render_vendor_risk_review()

    st.divider()

    # -----------------------------------------------------------------------
    # 5. Recent activity feed
    # -----------------------------------------------------------------------
    _render_recent_activity(events)
