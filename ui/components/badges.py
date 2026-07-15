"""
ui/components/badges.py — Reusable HTML status badges for Streamlit.

All display logic is centralised here so every page uses identical badge
styling and the mapping between domain values and colours lives in one place.

Usage:
    from ui.components.badges import outcome_badge, discount_badge, render_badge
    st.markdown(outcome_badge("STP"), unsafe_allow_html=True)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Colour map — outcome → (background, text, label)
# ---------------------------------------------------------------------------

_OUTCOME_STYLES: dict[str, tuple[str, str, str]] = {
    "STP":                ("#1e7e34", "#ffffff", "✅ Approved (STP)"),
    "EXCEPTION":          ("#c0392b", "#ffffff", "⚠️ Exception"),
    "NEEDS_REEXTRACTION": ("#7f8c8d", "#ffffff", "🔄 Needs Re-extraction"),
    "APPROVED":           ("#1e7e34", "#ffffff", "✅ Human Approved"),
    "REJECTED":           ("#c0392b", "#ffffff", "❌ Rejected"),
}

_DISCOUNT_STYLES: dict[str, tuple[str, str, str]] = {
    "TAKE_DISCOUNT":  ("#1565c0", "#ffffff", "💰 Take Discount"),
    "HOLD_TO_NET":    ("#f57c00", "#ffffff", "⏸ Hold to Net"),
    "WINDOW_MISSED":  ("#7f8c8d", "#ffffff", "🕐 Window Missed"),
    "NO_DISCOUNT":    ("#546e7a", "#ffffff", "— No Discount"),
}

_CHECK_STYLES: dict[bool, tuple[str, str, str]] = {
    True:  ("#2e7d32", "#ffffff", "✓ Pass"),
    False: ("#b71c1c", "#ffffff", "✗ Fail"),
}


def _badge_html(bg: str, fg: str, label: str) -> str:
    """Render a pill-shaped badge as an HTML span."""
    return (
        f'<span style="'
        f"background-color:{bg};"
        f"color:{fg};"
        f"padding:3px 10px;"
        f"border-radius:12px;"
        f"font-size:0.85rem;"
        f"font-weight:600;"
        f"display:inline-block;"
        f"margin:2px 0;"
        f'">{label}</span>'
    )


def outcome_badge(outcome: str) -> str:
    """Return HTML badge for a pipeline outcome string ('STP', 'EXCEPTION', etc.)."""
    bg, fg, label = _OUTCOME_STYLES.get(
        outcome.upper(),
        ("#546e7a", "#ffffff", outcome),
    )
    return _badge_html(bg, fg, label)


def discount_badge(recommendation: str) -> str:
    """Return HTML badge for a discount recommendation string."""
    bg, fg, label = _DISCOUNT_STYLES.get(
        recommendation.upper(),
        ("#546e7a", "#ffffff", recommendation),
    )
    return _badge_html(bg, fg, label)


def check_badge(passed: bool) -> str:
    """Return HTML badge for a boolean pass/fail check."""
    bg, fg, label = _CHECK_STYLES[passed]
    return _badge_html(bg, fg, label)


def reason_badge(reason_code: str) -> str:
    """Return HTML badge for an exception reason code."""
    return _badge_html("#8d1f1f", "#ffffff", f"⚠ {reason_code.replace('_', ' ').title()}")


def render_badge(text: str, colour: str = "#546e7a") -> str:
    """Render a free-form badge with a custom background colour (hex)."""
    return _badge_html(colour, "#ffffff", text)
