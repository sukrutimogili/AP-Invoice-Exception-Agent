"""
ui/pages/system_info.py — System Information page.

Displays runtime configuration, model information, prompt version, and
service health.  Never exposes secrets (API keys are masked).

All data sourced from:
  - app/config.py → get_settings()
  - extraction/prompts/v1_extract.md → prompt version header
  - app/main.py → FastAPI app metadata
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st

from app.config import get_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROMPT_PATH = Path(__file__).parent.parent.parent / "extraction" / "prompts" / "v1_extract.md"


def _prompt_version() -> tuple[str, str]:
    """Return (version_string, sha256_hex) from the extraction prompt file."""
    if not _PROMPT_PATH.exists():
        return ("not found", "—")
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    # Version is on the second line: "# Version: v1"
    for line in text.splitlines():
        if line.strip().lower().startswith("# version:"):
            version = line.split(":", 1)[1].strip()
            sha = hashlib.sha256(text.encode()).hexdigest()[:16]
            return (version, sha)
    sha = hashlib.sha256(text.encode()).hexdigest()[:16]
    return ("unknown", sha)


def _mask_key(key: str) -> str:
    """Return a masked API key showing only first 8 and last 4 characters."""
    if len(key) <= 12:
        return "sk-or-v1-***"
    return f"{key[:8]}…{key[-4:]}"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def render() -> None:
    st.title("⚙️ System Information")
    st.markdown(
        "Runtime configuration and service health.  "
        "Sensitive values (API keys) are masked."
    )

    try:
        settings = get_settings()
        config_ok = True
    except Exception as e:
        st.error(f"Configuration error: {e}")
        return

    # -----------------------------------------------------------------------
    # Health indicators
    # -----------------------------------------------------------------------
    st.subheader("🟢 Service Health")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.success("✅ Configuration loaded")

    with col2:
        # Check API key is set (not placeholder)
        key = settings.openrouter_api_key
        if key and key != "sk-or-v1-replace-me-with-your-real-openrouter-api-key":
            st.success("✅ API key configured")
        else:
            st.error("❌ API key not set")

    with col3:
        # Check audit log is reachable
        try:
            import audit.writer as aw
            aw.get_all_events()
            st.success("✅ Audit log active")
        except Exception:
            st.error("❌ Audit log error")

    # -----------------------------------------------------------------------
    # LLM configuration
    # -----------------------------------------------------------------------
    st.divider()
    st.subheader("🤖 LLM Configuration")

    prompt_ver, prompt_sha = _prompt_version()

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown(f"**Provider:** OpenRouter")
        st.markdown(f"**Base URL:** `{settings.openrouter_base_url}`")
        st.markdown(f"**API Key:** `{_mask_key(settings.openrouter_api_key)}`")
    with col_b:
        st.markdown(f"**Prompt version:** `{prompt_ver}`")
        st.markdown(f"**Prompt SHA-256 (truncated):** `{prompt_sha}`")

    st.markdown("**Fallback model chain:**")
    for i, model in enumerate(settings.openrouter_fallback_chain, 1):
        st.markdown(f"{i}. `{model}`")

    # -----------------------------------------------------------------------
    # Business logic thresholds
    # -----------------------------------------------------------------------
    st.divider()
    st.subheader("📊 Business Logic Thresholds")

    col_c, col_d, col_e = st.columns(3)
    with col_c:
        st.metric(
            "Approval Threshold",
            f"${settings.approval_threshold_default:,.2f}",
            help="Invoices at or above this amount require approval on file (FR-2.4).",
        )
    with col_d:
        st.metric(
            "Discount Hurdle Rate",
            f"{settings.discount_hurdle_rate_default * 100:.2f}%",
            help="Minimum annualized return to recommend taking a discount (FR-7.2).",
        )
    with col_e:
        st.metric(
            "Match Tolerance",
            f"{settings.match_tolerance_percent:.2f}%",
            help="Maximum variance % before a line is flagged as a mismatch (FR-2.2).",
        )

    # -----------------------------------------------------------------------
    # Database
    # -----------------------------------------------------------------------
    st.divider()
    st.subheader("🗄️ Database")
    st.markdown(f"**Connection:** `{settings.database_url}`")
    st.caption(
        "Phase 9 uses an in-process store (no active DB writes).  "
        "Swap `DATABASE_URL` to a Postgres DSN to upgrade without code changes."
    )

    # -----------------------------------------------------------------------
    # Audit & log
    # -----------------------------------------------------------------------
    st.divider()
    st.subheader("📋 Runtime State")

    import audit.writer as aw
    from api.payments import list_payment_schedules
    from api.exceptions import _exception_store

    events = aw.get_all_events()
    payments = list_payment_schedules()
    exceptions = list(_exception_store.values())

    col_f, col_g, col_h = st.columns(3)
    col_f.metric("Audit Events", len(events))
    col_g.metric("Payment Schedules", len(payments))
    col_h.metric("Open Exceptions", len(exceptions))

    st.caption(
        "These counts reflect in-process state since the last server start.  "
        "They reset on process restart until a persistent DB store is wired in."
    )

    # -----------------------------------------------------------------------
    # Logging
    # -----------------------------------------------------------------------
    st.divider()
    st.subheader("📝 Logging")
    st.markdown(f"**Log level:** `{settings.log_level}`")
    st.caption(
        "Structured JSON logging is enabled on all state transitions.  "
        "Change `LOG_LEVEL` in `.env` to adjust verbosity."
    )
