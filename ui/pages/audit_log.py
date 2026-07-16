"""
ui/pages/audit_log.py — Audit Log page.

Displays all audit events from audit/writer.py with live search and filter.
All data comes from audit_writer.get_all_events() — no business logic here.

Features:
  - Search by invoice number, vendor name, PO reference
  - Filter by event type (outcome)
  - Expand each record to view complete payload
  - Status badges per event type
  - Investigate button on EXCEPTION_RAISED events — calls the exception
    triage agent and renders root cause, recommendation, confidence, and the
    historical context it used directly above the approve/reject controls.
    The human still clicks approve/reject themselves — the investigation result
    is advisory only.
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

import audit.writer as audit_writer
from models.enums import AuditEventType
from ui.components.badges import AUDIT_EVENT_COLOURS, render_badge
from ui.components.theme import inject_theme


# ---------------------------------------------------------------------------
# Event-type → badge colour mapping  (sourced from badges.py palette)
# ---------------------------------------------------------------------------

_EVENT_COLOURS: dict[str, str] = {
    **AUDIT_EVENT_COLOURS,
    AuditEventType.INVESTIGATION_COMPLETED.value: "#4527A0",  # purple — agent output
}

# Short text labels for each event type — no emoji
_EVENT_LABELS: dict[str, str] = {
    AuditEventType.INVOICE_RECEIVED.value:          "Received",
    AuditEventType.EXTRACTION_SUCCEEDED.value:      "Extracted",
    AuditEventType.EXTRACTION_FAILED.value:         "Extraction Failed",
    AuditEventType.MATCHING_COMPLETED.value:        "Matched",
    AuditEventType.STP_APPROVED.value:              "STP Approved",
    AuditEventType.EXCEPTION_RAISED.value:          "Exception",
    AuditEventType.HUMAN_OVERRIDE_APPROVED.value:   "Human Approved",
    AuditEventType.HUMAN_REJECTED.value:            "Rejected",
    AuditEventType.PAYMENT_SCHEDULED.value:         "Payment Scheduled",
    AuditEventType.DISCOUNT_EVALUATED.value:        "Discount Evaluated",
    AuditEventType.INVESTIGATION_COMPLETED.value:   "Agent Investigation",
}

# Confidence level → colour for the investigation result callout
_CONFIDENCE_COLOURS: dict[str, str] = {
    "high":   "#1A6B2A",   # green
    "medium": "#B45309",   # amber
    "low":    "#C0392B",   # red
}

# recommended_action → display label (plain text, no emoji)
_ACTION_LABELS: dict[str, str] = {
    "APPROVE_OVERRIDE":           "Approve with Override",
    "REJECT":                     "Reject",
    "REQUEST_CORRECTED_DOCUMENT": "Request Corrected Document",
    "ESCALATE":                   "Escalate",
}


def _event_badge(event_type: str) -> str:
    colour = _EVENT_COLOURS.get(event_type, "#546e7a")
    label = _EVENT_LABELS.get(event_type, event_type.replace("_", " ").title())
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
# Investigation result renderer
# ---------------------------------------------------------------------------


def _render_investigation_result(invoice_id: str) -> None:
    """
    Render the ExceptionInvestigation result stored in session state for invoice_id.

    Shows root cause, recommendation badge, confidence, and supporting context
    items.  Placed ABOVE the approve/reject controls so the reviewer sees the
    triage analysis before acting.
    """
    result = st.session_state.get(f"investigation_{invoice_id}")
    if result is None:
        return

    st.markdown("---")
    st.markdown("#### Agent Investigation Result")
    st.caption(
        "This analysis was produced by the exception triage agent from exception data, "
        "vendor history, and the audit trail.  It is advisory only — you retain full "
        "authority to approve or reject."
    )

    # Root cause
    st.markdown(f"**Root cause:** {result.root_cause_summary}")

    # Recommendation badge
    action = result.recommended_action
    action_label = _ACTION_LABELS.get(action, action)
    colour = {
        "APPROVE_OVERRIDE": "#1A6B2A",
        "REJECT": "#C0392B",
        "REQUEST_CORRECTED_DOCUMENT": "#2563A8",
        "ESCALATE": "#B45309",
    }.get(action, "#4B5563")
    st.markdown(
        render_badge(f"Recommended: {action_label}", colour),
        unsafe_allow_html=True,
    )

    # Confidence
    conf = result.confidence
    conf_colour = _CONFIDENCE_COLOURS.get(conf, "#4B5563")
    st.markdown(
        render_badge(f"Confidence: {conf.upper()}", conf_colour),
        unsafe_allow_html=True,
    )

    # Supporting context
    if result.supporting_context:
        st.markdown("**Evidence used:**")
        for item in result.supporting_context:
            st.markdown(f"- {item}")

    st.markdown("---")


# ---------------------------------------------------------------------------
# Exception action controls (approve / reject)
# ---------------------------------------------------------------------------


def _render_exception_controls(invoice_id: str) -> None:
    """
    Render the human approve/reject controls for an open exception.

    These controls call the API endpoints; the actual business logic lives in
    api/exceptions.py.  This is display-only wiring.
    """
    from api.exceptions import approve_exception, reject_exception
    from models.exception_record import HumanResolutionUpdate
    from models.enums import HumanAction

    st.markdown("#### Human Review Controls")
    st.caption(
        "You are the decision-maker.  The agent recommendation above (if any) is advisory."
    )

    col_approve, col_reject = st.columns(2)

    with col_approve:
        actor_approve = st.text_input(
            "Your identity (approver)",
            key=f"actor_approve_{invoice_id}",
            placeholder="e.g. jane.doe@company.com",
        )
        notes_approve = st.text_area(
            "Override notes",
            key=f"notes_approve_{invoice_id}",
            placeholder="Reason for approving despite exception…",
            height=80,
        )
        if st.button(
            "Approve with Override",
            key=f"approve_{invoice_id}",
            type="primary",
            disabled=not actor_approve.strip(),
        ):
            try:
                from db.session import get_session
                with get_session() as session:
                    update = HumanResolutionUpdate(
                        human_action=HumanAction.APPROVE_OVERRIDE,
                        actor_id=actor_approve.strip(),
                        resolution_notes=notes_approve.strip() or None,
                    )
                    approve_exception(invoice_id, update, session)
                st.success(f"Invoice {invoice_id} approved with override by {actor_approve}.")
                st.rerun()
            except Exception as exc:
                st.error(f"Approval failed: {exc}")

    with col_reject:
        actor_reject = st.text_input(
            "Your identity (rejector)",
            key=f"actor_reject_{invoice_id}",
            placeholder="e.g. john.smith@company.com",
        )
        notes_reject = st.text_area(
            "Rejection notes",
            key=f"notes_reject_{invoice_id}",
            placeholder="Reason for rejecting…",
            height=80,
        )
        if st.button(
            "Reject",
            key=f"reject_{invoice_id}",
            disabled=not actor_reject.strip(),
        ):
            try:
                from db.session import get_session
                with get_session() as session:
                    update = HumanResolutionUpdate(
                        human_action=HumanAction.REJECT,
                        actor_id=actor_reject.strip(),
                        resolution_notes=notes_reject.strip() or None,
                    )
                    reject_exception(invoice_id, update, session)
                st.success(f"Invoice {invoice_id} rejected by {actor_reject}.")
                st.rerun()
            except Exception as exc:
                st.error(f"Rejection failed: {exc}")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def render() -> None:
    inject_theme()
    st.title("Audit Log")
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
    # Pre-compute which invoice IDs have been EXCEPTION_RAISED but not yet
    # HUMAN_OVERRIDE_APPROVED or HUMAN_REJECTED (i.e. still open)
    resolved_ids: set[str] = {
        e["invoice_id"]
        for e in all_events
        if e.get("event_type") in (
            AuditEventType.HUMAN_OVERRIDE_APPROVED.value,
            AuditEventType.HUMAN_REJECTED.value,
        )
    }
    exception_ids: set[str] = {
        e["invoice_id"]
        for e in all_events
        if e.get("event_type") == AuditEventType.EXCEPTION_RAISED.value
    }
    open_exception_ids = exception_ids - resolved_ids

    for event in reversed(filtered):
        event_type = event.get("event_type", "UNKNOWN")
        invoice_id = event.get("invoice_id", "—")
        invoice_number = event.get("invoice_number") or "—"
        vendor_name = event.get("vendor_name") or "—"
        po_ref = event.get("po_reference") or "—"
        created_at = str(event.get("created_at", ""))
        actor_id = event.get("actor_id") or "—"

        # Build a short summary line for the expander header
        event_label = _EVENT_LABELS.get(event_type, event_type.replace("_", " ").title())
        header = (
            f"**{event_label}** "
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

            # -------------------------------------------------------------------
            # Investigate button — only on EXCEPTION_RAISED for open exceptions
            # -------------------------------------------------------------------
            if (
                event_type == AuditEventType.EXCEPTION_RAISED.value
                and invoice_id in open_exception_ids
            ):
                st.markdown("---")

                # Show any previously computed investigation result first
                _render_investigation_result(invoice_id)

                # Investigate button — triggers the triage agent
                investigate_key = f"investigate_{invoice_id}"
                if st.button(
                    "Investigate",
                    key=investigate_key,
                    help=(
                        "Run the exception triage agent — analyses reason codes, "
                        "vendor history, and the audit trail to produce a recommendation. "
                        "The agent does not approve or reject; you do."
                    ),
                ):
                    with st.spinner("Investigating exception — querying LLM…"):
                        try:
                            from agents.exception_triage_agent import (
                                ExceptionInvestigation,
                                InvestigationError,
                                investigate_exception,
                            )
                            result = investigate_exception(invoice_id)
                            st.session_state[f"investigation_{invoice_id}"] = result
                            st.rerun()
                        except InvestigationError as exc:
                            st.error(f"Investigation failed: {exc}")
                        except Exception as exc:
                            st.error(f"Unexpected error during investigation: {exc}")

                # Approve / reject controls (below investigation result, above divider)
                _render_exception_controls(invoice_id)
