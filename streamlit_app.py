"""
streamlit_app.py — LedgerGate-Agent Streamlit UI entry point.

Placed at the project root so that Python's import system resolves all
package imports (app, models, extraction, matching, routing, discount, audit,
api, ui) against the project root without any sys.path manipulation.

When Streamlit runs a script it inserts the *directory containing the script*
as the first entry on sys.path.  With this file at the project root, that
directory IS the project root, so ``import app`` resolves to ``app/``
(a real package) rather than shadowing it.

Launch:
    streamlit run streamlit_app.py

Or explicitly with the venv interpreter:
    .venv/Scripts/streamlit run streamlit_app.py   (Windows)
    .venv/bin/streamlit run streamlit_app.py       (Linux/macOS)
"""

from __future__ import annotations

import streamlit as st

# ---------------------------------------------------------------------------
# Page config — must be the very first Streamlit call in the entry point.
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="LedgerGate-Agent",
    page_icon="🧾",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Import page modules.
# All domain imports inside these modules resolve cleanly because sys.path[0]
# is now the project root (the directory containing this file).
# ---------------------------------------------------------------------------
from ui.pages import audit_log, discount_optimization, invoice_processing, system_info

# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("## 🧾 LedgerGate-Agent")
    st.caption("AP Invoice Exception Agent")
    st.divider()

    page = st.radio(
        "Navigate",
        options=[
            "📋 Invoice Processing",
            "📖 Audit Log",
            "💰 Discount Optimization",
            "⚙️ System Information",
        ],
        label_visibility="collapsed",
    )

    st.divider()
    st.caption(
        "**Architecture:**  \n"
        "Presentation layer only — all business logic lives in the domain modules.  \n\n"
        "**REST API docs:**  \n"
        "`uvicorn app.main:app --reload`  \n"
        "then open `http://localhost:8000/docs`"
    )

# ---------------------------------------------------------------------------
# Route to the selected page
# ---------------------------------------------------------------------------
if page == "📋 Invoice Processing":
    invoice_processing.render()

elif page == "📖 Audit Log":
    audit_log.render()

elif page == "💰 Discount Optimization":
    discount_optimization.render()

elif page == "⚙️ System Information":
    system_info.render()
