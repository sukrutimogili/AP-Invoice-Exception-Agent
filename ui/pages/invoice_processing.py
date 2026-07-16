"""
ui/pages/invoice_processing.py — Invoice Processing page.

Provides two submission modes:
  1. Document Upload — paste or upload a text invoice → LLM extraction pipeline.
  2. Structured Submit — fill in a form → direct pipeline (no LLM extraction).

Both modes display:
  - Extracted invoice fields in a clean table
  - Matching check results with pass/fail badges
  - Routing decision badge
  - Payment schedule (if STP)
  - Exception reason codes (if EXCEPTION)
  - Discount recommendation (if applicable)

Architecture: zero business logic here — all work delegated to
ui/components/pipeline_runner.py which calls the existing domain modules.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import streamlit as st

from ui.components.badges import (
    check_badge,
    discount_badge,
    outcome_badge,
    reason_badge,
)
from ui.components.pipeline_runner import (
    PipelineResult,
    run_extraction_pipeline,
    run_extraction_pipeline_with_documents,
    run_submit_pipeline,
)
from ui.components.theme import inject_theme
from models.contract import ContractCreate, ContractLineItemCreate, DiscountTermSchema
from models.invoice import InvoiceCreate, InvoiceLineItemCreate
from models.purchase_order import POLineItemCreate, PurchaseOrderCreate
from models.vendor import VendorCreate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_decimal(val: str, default: str = "0") -> Decimal:
    try:
        return Decimal(val.strip())
    except (InvalidOperation, AttributeError):
        return Decimal(default)


def _render_result(result: PipelineResult) -> None:
    """Render the full PipelineResult — shared between both submission modes."""

    st.divider()
    st.subheader("Processing Result")

    # --- Outcome badge ---
    st.markdown(
        outcome_badge(result.outcome),
        unsafe_allow_html=True,
    )
    st.caption(f"Invoice ID: `{result.invoice_id}`  ·  Processed at: {result.processed_at}")

    if result.outcome == "ERROR":
        st.error(result.error_message or "An unexpected error occurred.")
        return

    if result.outcome == "NEEDS_REEXTRACTION":
        st.error(
            f"Extraction failed — this invoice cannot be processed.  \n"
            f"**Reason:** `{result.extraction_failure_reason}`  \n\n"
            "Please check that the document is a valid UTF-8 text file containing "
            "all required invoice fields."
        )
        return

    # --- PO / contract extraction warnings (non-blocking; shown on any outcome) ---
    if result.po_extraction_warning:
        st.warning(
            f"**Purchase Order not extracted** — the PO document was uploaded "
            f"but extraction failed, so the invoice was processed without it.  \n"
            f"**Detail:** {result.po_extraction_warning}"
        )
    if result.contract_extraction_warning:
        st.warning(
            f"**Contract not extracted** — the contract document was uploaded "
            f"but extraction failed, so the invoice was processed without it.  \n"
            f"**Detail:** {result.contract_extraction_warning}"
        )

    # --- Extracted fields ---
    if result.invoice_fields:
        with st.expander("Extracted Invoice Fields", expanded=True):
            fields = result.invoice_fields
            col1, col2 = st.columns(2)
            with col1:
                st.markdown(f"**Invoice #:** {fields.get('invoice_number', '—')}")
                st.markdown(f"**Vendor:** {fields.get('vendor_name', '—')}")
                st.markdown(f"**Invoice Date:** {fields.get('invoice_date', '—')}")
                st.markdown(f"**Due Date:** {fields.get('due_date', '—')}")
            with col2:
                st.markdown(f"**PO Reference:** {fields.get('po_reference', '—')}")
                st.markdown(f"**Contract Ref:** {fields.get('contract_reference', '—')}")
                st.markdown(f"**Payment Terms:** {fields.get('payment_terms', '—')}")
                st.markdown(f"**Grand Total:** ${fields.get('grand_total', '—')}")

            # Line items table
            line_items = fields.get("line_items", [])
            if line_items:
                st.markdown("**Line Items:**")
                import pandas as pd
                df = pd.DataFrame(line_items)
                df.columns = [c.replace("_", " ").title() for c in df.columns]
                st.dataframe(df, use_container_width=True, hide_index=True)

    # --- Match checks ---
    if result.match_checks:
        with st.expander("Matching Checks (FR-3.1)", expanded=True):
            cols = st.columns(4)
            for i, (check_name, passed) in enumerate(result.match_checks.items()):
                with cols[i % 4]:
                    st.markdown(
                        f"**{check_name}**  \n{check_badge(passed)}",
                        unsafe_allow_html=True,
                    )

    # --- STP path ---
    if result.outcome == "STP":
        sched = result.payment_schedule
        if sched:
            with st.expander("Payment Schedule", expanded=True):
                col1, col2 = st.columns(2)
                with col1:
                    st.metric("Scheduled Date", sched.get("scheduled_date", "—"))
                    st.metric("Amount", f"${sched.get('amount', '—')}")
                with col2:
                    discount_taken = sched.get("discount_taken", False)
                    st.metric(
                        "Discount Applied",
                        "Yes" if discount_taken else "No",
                    )
                    if discount_taken and sched.get("discount_amount"):
                        st.metric("Discount Amount", f"${sched['discount_amount']}")

        # Discount recommendation badge
        if result.discount_recommendation:
            st.markdown("**Discount Recommendation:**")
            st.markdown(
                discount_badge(result.discount_recommendation),
                unsafe_allow_html=True,
            )

    # --- EXCEPTION path ---
    if result.outcome == "EXCEPTION":
        with st.expander("Exception Details", expanded=True):
            st.markdown("**This invoice has been routed to the human review queue.**")
            st.markdown("**Reason Codes:**")
            for code in result.exception_reasons:
                st.markdown(reason_badge(code), unsafe_allow_html=True)
            st.info(
                "Use the **Audit** page to view the full decision trail.  "
                "Use `POST /exceptions/{invoice_id}/approve` or `/reject` "
                "to resolve this exception."
            )

        # --- Low-confidence fields (shown only when reason is LOW_CONFIDENCE_EXTRACTION) ---
        if result.low_confidence_fields:
            with st.expander("Low-Confidence Fields — Human Verification Required", expanded=True):
                st.warning(
                    "The LLM extracted the following fields but flagged them as **uncertain**.  "
                    "The values come directly from the document — nothing was invented — but the "
                    "reading may be ambiguous (e.g. an unusual number format, unclear column "
                    "labelling, or an ambiguous date).  "
                    "Please verify each value against the original document before approving."
                )
                st.markdown("**Flagged fields and extracted values:**")

                import pandas as pd

                rows = [
                    {"Field": k, "Extracted Value": v}
                    for k, v in result.low_confidence_fields.items()
                ]
                df = pd.DataFrame(rows)
                st.dataframe(df, use_container_width=True, hide_index=True)

        # --- Document Conflict diff (rendered separately, always expanded) ---
        if result.po_conflict_diff:
            _render_conflict_diff("PO", result.po_conflict_diff)

        if result.contract_conflict_diff:
            _render_conflict_diff("Contract", result.contract_conflict_diff)


# ---------------------------------------------------------------------------
# Document conflict diff renderer
# ---------------------------------------------------------------------------


def _render_conflict_diff(doc_type: str, diff: dict) -> None:
    """
    Render a Document Conflict expander showing every disagreeing field.

    Args:
        doc_type: "PO" or "Contract" (display label only).
        diff:     dict[field_name, {"existing": str, "incoming": str}]
    """
    with st.expander(f"Document Conflict — {doc_type}", expanded=True):
        st.warning(
            f"The uploaded **{doc_type}** document disagrees with the record "
            f"already stored in the database.  The existing row has **not** been "
            f"overwritten.  A human reviewer must approve or reject this invoice "
            f"before any changes take effect."
        )
        st.markdown("**Fields that differ:**")

        import pandas as pd

        rows = []
        for field_name, values in diff.items():
            rows.append(
                {
                    "Field": field_name,
                    "Stored (existing)": values.get("existing", "—") or "—",
                    "Uploaded (incoming)": values.get("incoming", "—") or "—",
                }
            )
        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.markdown("_(no field-level detail available)_")


# ---------------------------------------------------------------------------
# Mode 1 — Document Upload (LLM extraction)
# ---------------------------------------------------------------------------


def _upload_widget(label: str, key_prefix: str) -> str | None:
    """
    Render a paste-or-upload widget for one document type.

    Returns the extracted text string, or None if nothing was provided.
    Uses key_prefix to namespace all Streamlit widget keys and avoid
    conflicts between the Invoice, PO, and Contract widgets on the same page.
    """
    from extraction.document_loader import DocumentLoadError, load_document_text

    upload_method = st.radio(
        f"{label} — input method",
        ["Paste text", "Upload file"],
        horizontal=True,
        key=f"{key_prefix}_method",
    )

    text: str | None = None

    if upload_method == "Paste text":
        text = st.text_area(
            f"{label} text",
            height=180,
            placeholder=f"Paste the full {label.lower()} text here…",
            key=f"{key_prefix}_paste",
        )
        if text and not text.strip():
            text = None
    else:
        uploaded = st.file_uploader(
            f"Upload {label} file",
            type=["txt", "pdf"],
            help=(
                "Plain-text (.txt, UTF-8) or PDF (.pdf), max 1 MB.  "
                "PDFs must have an embedded text layer."
            ),
            key=f"{key_prefix}_upload",
        )
        if uploaded is not None:
            if uploaded.size > 1_048_576:
                st.error(f"{label}: file exceeds 1 MB limit.")
            else:
                raw_bytes = uploaded.read()
                try:
                    text = load_document_text(
                        raw_bytes,
                        filename=uploaded.name,
                        content_type=uploaded.type or None,
                    )
                    st.success(f"Loaded **{uploaded.name}** ({uploaded.size:,} bytes)")
                except DocumentLoadError as exc:
                    st.error(f"Could not load {label}: {exc.message}")

    return text or None


def _render_upload_tab() -> None:
    st.markdown(
        "Upload plain-text or PDF documents. The system extracts all fields using "
        "the LLM extraction pipeline, then runs matching and routing.  "
        "PO and Contract documents are optional — if omitted, the system falls "
        "back to database lookup using the references on the invoice."
    )
    st.caption("Requires a valid `OPENROUTER_API_KEY` in your `.env` file.")

    # ---- Invoice (required) ------------------------------------------------
    st.subheader("Invoice", divider="gray")
    invoice_text = _upload_widget("Invoice", "inv")

    # ---- Purchase Order (optional) -----------------------------------------
    st.subheader("Purchase Order", divider="gray")
    st.caption(
        "Optional. If supplied, the extracted PO is upserted into the database.  "
        "A conflict with an existing record routes the invoice to EXCEPTION."
    )
    po_text = _upload_widget("Purchase Order", "po")

    # ---- Contract (optional) -----------------------------------------------
    st.subheader("Contract", divider="gray")
    st.caption(
        "Optional. If supplied, the extracted contract is upserted into the database.  "
        "A conflict with an existing record routes the invoice to EXCEPTION."
    )
    contract_text = _upload_widget("Contract", "contract")

    # ---- Process button ----------------------------------------------------
    if st.button("Extract and Process", type="primary", disabled=not invoice_text):
        if not invoice_text or not invoice_text.strip():
            st.warning("Please provide invoice text before processing.")
            return

        with st.spinner("Running LLM extraction and processing pipeline…"):
            result = run_extraction_pipeline_with_documents(
                invoice_text=invoice_text,
                po_text=po_text,
                contract_text=contract_text,
            )

        st.session_state["last_result"] = result
        _render_result(result)


# ---------------------------------------------------------------------------
# Mode 2 — Structured Submit (form-based, no LLM)
# ---------------------------------------------------------------------------

def _render_submit_tab() -> None:
    st.markdown(
        "Fill in the invoice fields directly. The system runs matching and routing "
        "without LLM extraction. Use this for testing or system-integration scenarios."
    )

    with st.form("invoice_submit_form", clear_on_submit=False):
        st.subheader("Invoice Header")
        col1, col2 = st.columns(2)
        with col1:
            invoice_number = st.text_input("Invoice Number *", value="INV-2025-001")
            vendor_name = st.text_input("Vendor Name *", value="Acme Supplies Ltd")
            invoice_date = st.date_input("Invoice Date *", value=date.today() - timedelta(days=5))
            po_reference = st.text_input("PO Reference *", value="PO-2025-0100")
        with col2:
            contract_reference = st.text_input("Contract Reference *", value="CTR-2025-0018")
            due_date = st.date_input("Due Date *", value=date.today() + timedelta(days=25))
            payment_terms = st.text_input("Payment Terms *", value="Net 30")
            grand_total_str = st.text_input("Grand Total *", value="420.00")

        col3, col4 = st.columns(2)
        with col3:
            subtotal_str = st.text_input("Subtotal *", value="400.00")
        with col4:
            tax_str = st.text_input("Tax *", value="20.00")

        st.subheader("Line Items")
        st.caption("Enter one line item per row. Separate rows with the Add button.")
        n_lines = st.number_input("Number of line items", min_value=1, max_value=10, value=2)

        line_data = []
        for i in range(int(n_lines)):
            st.markdown(f"**Line {i + 1}**")
            lc1, lc2, lc3 = st.columns(3)
            with lc1:
                desc = st.text_input(f"Description", value=f"Item {i+1}", key=f"desc_{i}")
            with lc2:
                qty_s = st.text_input("Qty", value="10" if i == 0 else "1", key=f"qty_{i}")
            with lc3:
                up_s = st.text_input("Unit Price", value="38.00" if i == 0 else "20.00", key=f"up_{i}")
            line_data.append((i + 1, desc, qty_s, up_s))

        st.subheader("Context (optional — leave blank to trigger exceptions)")
        st.caption("Provide vendor / PO / contract to enable STP. Leave blank to test exception routing.")
        col5, col6 = st.columns(2)
        with col5:
            has_vendor = st.checkbox("Include vendor (active)", value=True)
            has_po = st.checkbox("Include purchase order", value=True)
        with col6:
            has_contract = st.checkbox("Include contract", value=True)
            approval_on_file = st.checkbox("Approval on file", value=False)

        discount_term_raw = st.text_input(
            "Discount term (optional)",
            value="2/10 net 30",
            help='e.g. "2/10 net 30" means 2% discount if paid within 10 days on net-30 terms.',
        )

        submitted = st.form_submit_button("Submit Invoice", type="primary")

    if submitted:
        # --- Build line items ---
        line_items = []
        for ln, desc, qty_s, up_s in line_data:
            qty = _safe_decimal(qty_s, "1")
            up = _safe_decimal(up_s, "0")
            amount = (qty * up).quantize(Decimal("0.01"))
            try:
                line_items.append(
                    InvoiceLineItemCreate(
                        line_number=ln,
                        description=desc or f"Item {ln}",
                        qty=qty,
                        unit_price=up,
                        amount=amount,
                    )
                )
            except Exception as e:
                st.error(f"Line item {ln} is invalid: {e}")
                return

        # --- Build InvoiceCreate ---
        try:
            invoice = InvoiceCreate(
                invoice_number=invoice_number,
                vendor_name=vendor_name,
                invoice_date=invoice_date,
                po_reference=po_reference,
                contract_reference=contract_reference,
                due_date=due_date,
                payment_terms=payment_terms,
                subtotal=_safe_decimal(subtotal_str),
                tax=_safe_decimal(tax_str),
                grand_total=_safe_decimal(grand_total_str, "1"),
                line_items=line_items,
            )
        except Exception as e:
            st.error(f"Invoice validation failed: {e}")
            return

        # --- Build optional mock entities ---
        vendor: VendorCreate | None = None
        po: PurchaseOrderCreate | None = None
        contract: ContractCreate | None = None

        if has_vendor:
            vendor = VendorCreate(
                vendor_code="ACME-001",
                name=vendor_name,
                is_active=True,
            )

        if has_po:
            po_lines = []
            for i, (ln, desc, qty_s, up_s) in enumerate(line_data):
                po_lines.append(
                    POLineItemCreate(
                        line_number=ln,
                        description=desc or f"Item {ln}",
                        qty=_safe_decimal(qty_s, "1"),
                        unit_price=_safe_decimal(up_s, "0"),
                    )
                )
            try:
                po = PurchaseOrderCreate(
                    po_number=po_reference,
                    vendor_id="vendor-uuid-001",
                    po_total=_safe_decimal(grand_total_str, "1"),
                    approval_threshold=Decimal("10000.00"),
                    line_items=po_lines,
                )
            except Exception as e:
                st.error(f"PO construction failed: {e}")
                return

        if has_contract:
            contract_lines = []
            for ln, desc, qty_s, up_s in line_data:
                contract_lines.append(
                    ContractLineItemCreate(
                        line_number=ln,
                        description=desc or f"Item {ln}",
                        unit_price=_safe_decimal(up_s, "0"),
                    )
                )

            # Parse discount term if provided
            discount_term: DiscountTermSchema | None = None
            if discount_term_raw.strip():
                from discount.parser import parse_discount_term
                discount_term = parse_discount_term(discount_term_raw.strip())

            try:
                contract = ContractCreate(
                    contract_reference=contract_reference,
                    vendor_id="vendor-uuid-001",
                    discount_term=discount_term,
                    line_items=contract_lines,
                )
            except Exception as e:
                st.error(f"Contract construction failed: {e}")
                return

        with st.spinner("Running matching and routing pipeline…"):
            result = run_submit_pipeline(
                invoice=invoice,
                vendor=vendor,
                po=po,
                contract=contract,
                approval_on_file=approval_on_file,
            )

        st.session_state["last_result"] = result
        _render_result(result)


# ---------------------------------------------------------------------------
# Page entry point
# ---------------------------------------------------------------------------

def render() -> None:
    inject_theme()
    st.title("Invoice Processing")
    st.markdown(
        "Submit an invoice for extraction, matching, routing, and scheduling.  "
        "Clean invoices are straight-through processed (STP); exceptions are "
        "routed to the human review queue."
    )

    tab1, tab2 = st.tabs(["Document Upload (LLM)", "Structured Submit (Form)"])

    with tab1:
        _render_upload_tab()

    with tab2:
        _render_submit_tab()

    # Re-show the last result if navigating back
    if "last_result" in st.session_state and st.session_state.get("show_last_result"):
        _render_result(st.session_state["last_result"])
