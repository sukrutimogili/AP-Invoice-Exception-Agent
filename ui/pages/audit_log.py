"""
ui/pages/audit_log.py — Audit Log page.

Displays all audit events from audit/writer.py with live search and filter.
All data comes from audit_writer.get_all_events() — no business logic here.

Features:
  - Search by invoice number, vendor name, PO reference
  - Filter by event type (outcome)
  - Expand each record to view complete payload
  - Status badges per event type
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

import audit.writer as audit_writer
from models.enums import AuditEventType
from ui.components.badges import render_badge


# ---------------------------------------------------------------------------
# Event-type → badge colour mapping
# ---------------------------------------------------------------------------

_EVENT_COLOURS: dict[str, str] = {
    AuditEventType.INVOICE_RECEIVED.value:        "#1565c0",
    AuditEventType.EXTRACTION_SUCCEEDED.value:    "#2e7d32",
    AuditEventType.EXTRACTION_FAILED.value:       "#b71c1c",
    AuditEventType.MATCHING_COMPLETED.value:      "#4527a0",
    AuditEventType.STP_APPROVED.value:            "#1e7e34",
    AuditEventType.EXCEPTION_RAISED.value:        "#c0392b",
    AuditEventType.HUMAN_OVERRIDE_APPROVED.value: "#0277bd",
    AuditEventType.HUMAN_REJECTED.value:          "#880e4f",
    AuditEventType.PAYMENT_SCHEDULED.value:       "#1b5e20",
    AuditEventType.DISCOUNT_EVALUATED.value:      "#e65100",
}

_EVENT_ICONS: dict[str, str] = {
    AuditEventType.INVOICE_RECEIVED.value:        "📥",
    AuditEventType.EXTRACTION_SUCCEEDED.value:    "✅",
    AuditEventType.EXTRACTION_FAILED.value:       "❌",
    AuditEventType.MATCHING_COMPLETED.value:      "🔍",
    AuditEventType.STP_APPROVED.value:            "💚",
    AuditEventType.EXCEPTION_RAISED.value:        "⚠️",
    AuditEventType.HUMAN_OVERRIDE_APPROVED.value: "👤",
    AuditEventType.HUMAN_REJECTED.value:          "🚫",
    AuditEventType.PAYMENT_SCHEDULED.value:       "💳",
    AuditEventType.DISCOUNT_EVALUATED.value:      "💰",
}


def _event_badge(event_type: str) -> str:
    colour = _EVENT_COLOURS.get(event_type, "#546e7a")
    icon = _EVENT_ICONS.get(event_type, "•")
    label = f"{icon} {event_type.replace('_', ' ').title()}"
    return render_badge(label, colour)


def _format_payload(payload_json: str | None) -> str:
    """Pretty-print the JSON payload string."""
    if not payload_json:
        return "(no payload)"
    try:
        data = json.loads(payload_json)
        return json.dumps(data, indent=2, default=str)
    except Exception:
        return payload_json


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def render() -> None:
    st.title("📖 Audit Log")
    st.markdown(
        "Append-only audit trail for every invoice processed.  "
        "All state transitions are recorded here (FR-6.1)."
    )

    # -----------------------------------------------------------------------
    # Load all events
    # -----------------------------------------------------------------------
    all_events: list[dict[str, Any]] = audit_writer.get_all_events()

    if not all_events:
        st.info(
            "No audit events recorded yet.  "
            "Go to **Invoice Processing** to submit an invoice."
        )
        return

    # -----------------------------------------------------------------------
    # Search & filter controls
    # -----------------------------------------------------------------------
    st.subheader("Search & Filter")
    col1, col2, col3 = st.columns(3)

    with col1:
        search_invoice = st.text_input(
            "Invoice number",
            placeholder="e.g. INV-2025-001",
        )
    with col2:
        search_vendor = st.text_input(
            "Vendor name",
            placeholder="Partial match",
        )
    with col3:
        search_po = st.text_input(
            "PO reference",
            placeholder="e.g. PO-2025-0100",
        )

    event_type_options = ["All"] + sorted({e.get("event_type", "") for e in all_events if e.get("event_type")})
    selected_type = st.selectbox("Event type", event_type_options)

    # -----------------------------------------------------------------------
    # Apply filters
    # -----------------------------------------------------------------------
    filtered = all_events

    if search_invoice.strip():
        filtered = [e for e in filtered if search_invoice.strip().lower() in (e.get("invoice_number") or "").lower()]

    if search_vendor.strip():
        filtered = [e for e in filtered if search_vendor.strip().lower() in (e.get("vendor_name") or "").lower()]

    if search_po.strip():
        filtered = [e for e in filtered if search_po.strip() == (e.get("po_reference") or "")]

    if selected_type != "All":
        filtered = [e for e in filtered if e.get("event_type") == selected_type]

    st.caption(f"Showing **{len(filtered)}** of **{len(all_events)}** events")

    if not filtered:
        st.warning("No events match your filters.")
        return

    # -----------------------------------------------------------------------
    # Summary metrics
    # -----------------------------------------------------------------------
    st.subheader("Summary")
    from collections import Counter
    type_counts = Counter(e.get("event_type", "UNKNOWN") for e in all_events)
    stp_count = type_counts.get(AuditEventType.STP_APPROVED.value, 0)
    exc_count = type_counts.get(AuditEventType.EXCEPTION_RAISED.value, 0)
    disc_count = type_counts.get(AuditEventType.DISCOUNT_EVALUATED.value, 0)
    total_count = len(all_events)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Events", total_count)
    m2.metric("STP Approved", stp_count)
    m3.metric("Exceptions Raised", exc_count)
    m4.metric("Discount Evaluated", disc_count)

    st.divider()

    # -----------------------------------------------------------------------
    # Event list — newest first
    # -----------------------------------------------------------------------
    for event in reversed(filtered):
        event_type = event.get("event_type", "UNKNOWN")
        invoice_id = event.get("invoice_id", "—")
        invoice_number = event.get("invoice_number") or "—"
        vendor_name = event.get("vendor_name") or "—"
        po_ref = event.get("po_reference") or "—"
        created_at = str(event.get("created_at", ""))
        actor_id = event.get("actor_id") or "—"

        # Build a short summary line for the expander header
        icon = _EVENT_ICONS.get(event_type, "•")
        header = (
            f"{icon} **{event_type.replace('_', ' ').title()}** "
            f"— Invoice `{invoice_number}` "
            f"· Vendor: {vendor_name} "
            f"· {created_at[:19] if created_at else ''}"
        )

        with st.expander(header, expanded=False):
            st.markdown(
                _event_badge(event_type),
                unsafe_allow_html=True,
            )

            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown(f"**Event ID:** `{event.get('id', '—')}`")
                st.markdown(f"**Invoice ID:** `{invoice_id}`")
                st.markdown(f"**Invoice #:** {invoice_number}")
                st.markdown(f"**Vendor:** {vendor_name}")
            with col_b:
                st.markdown(f"**PO Reference:** {po_ref}")
                st.markdown(f"**Actor:** {actor_id}")
                st.markdown(f"**Timestamp:** {created_at}")

            st.markdown("**Payload:**")
            payload_str = _format_payload(event.get("payload_json"))
            st.code(payload_str, language="json")
