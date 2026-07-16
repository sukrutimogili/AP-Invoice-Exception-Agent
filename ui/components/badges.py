"""
ui/components/badges.py — Reusable HTML status badges for Streamlit.

All display logic is centralised here so every page uses identical badge
styling.  Badge pills are self-contained HTML spans with their own background
and foreground colours — they read correctly on any surface (card, gradient,
sidebar) because they do not inherit page-level colours.

  App theme palette  (.streamlit/config.toml + theme.py)
  ───────────────────────────────────────────────────────
  #050B1A  deep navy      — gradient darkest stop / sidebar background
  #0D1B3E  navy mid       — body text on card surfaces / hero band
  #1E5FD9  electric blue  — primary accent / gradient mid stop
  #F0F4FF  near-white     — gradient lightest stop / page bg
  #FAFBFF  card surface   — solid white-ish surface behind all content

  Badge semantic colours (WCAG contrast: white text on badge bg, verified)
  ─────────────────────────────────────────────────────────────────────────
  SUCCESS / STP / Pass    #1A6B2A — dark green pill, white text  6.6:1  AA
  EXCEPTION / Fail        #C0392B — red pill, white text         5.4:1  AA
  WARNING / HOLD_TO_NET   #B45309 — amber pill, white text       5.0:1  AA
  INFO / neutral          #2563A8 — blue pill, white text        6.1:1  AA
  MUTED / inactive        #4B5563 — grey pill, white text        7.6:1  AA
  REASON / alert-secondary #7C2020 — dark-red pill, white text  10.1:1  AA

  No emoji are used in this module.  Pass/fail check badges use inline SVG
  icons for clear visual distinction without Unicode glyphs.  All other
  labels use plain descriptive text.

Usage:
    from ui.components.badges import outcome_badge, discount_badge, render_badge
    st.markdown(outcome_badge("STP"), unsafe_allow_html=True)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Semantic palette — kept here so changes propagate everywhere in one edit
# ---------------------------------------------------------------------------

_SUCCESS  = "#1A6B2A"   # dark green — STP / Pass / Approved
_ERROR    = "#C0392B"   # red        — Exception / Fail / Rejected
_WARNING  = "#B45309"   # amber      — hold-to-net / window-missed / re-extraction
_INFO     = "#2563A8"   # blue       — take-discount / informational
_MUTED    = "#4B5563"   # grey       — no-discount / unknown / neutral
_REASON   = "#7C2020"   # dark-red   — exception reason codes

_TEXT_ON_DARK = "#F5F5F5"   # off-white — readable on all the dark pill backgrounds

# ---------------------------------------------------------------------------
# Colour maps — outcome → (background, text, label)
# ---------------------------------------------------------------------------

_OUTCOME_STYLES: dict[str, tuple[str, str, str]] = {
    "STP":                (_SUCCESS, _TEXT_ON_DARK, "Approved (STP)"),
    "EXCEPTION":          (_ERROR,   _TEXT_ON_DARK, "Exception"),
    "NEEDS_REEXTRACTION": (_WARNING, _TEXT_ON_DARK, "Needs Re-extraction"),
    "APPROVED":           (_SUCCESS, _TEXT_ON_DARK, "Human Approved"),
    "REJECTED":           (_ERROR,   _TEXT_ON_DARK, "Rejected"),
    "ERROR":              (_ERROR,   _TEXT_ON_DARK, "Error"),
}

_DISCOUNT_STYLES: dict[str, tuple[str, str, str]] = {
    "TAKE_DISCOUNT": (_INFO,    _TEXT_ON_DARK, "Take Discount"),
    "HOLD_TO_NET":   (_WARNING, _TEXT_ON_DARK, "Hold to Net"),
    "WINDOW_MISSED": (_MUTED,   _TEXT_ON_DARK, "Window Missed"),
    "NO_DISCOUNT":   (_MUTED,   _TEXT_ON_DARK, "No Discount"),
}

# Audit event-type → badge background (for audit_log and dashboard pages)
AUDIT_EVENT_COLOURS: dict[str, str] = {
    "INVOICE_RECEIVED":        _INFO,
    "EXTRACTION_SUCCEEDED":    _SUCCESS,
    "EXTRACTION_FAILED":       _ERROR,
    "MATCHING_COMPLETED":      "#4527A0",   # purple — distinct neutral state
    "STP_APPROVED":            _SUCCESS,
    "EXCEPTION_RAISED":        _ERROR,
    "HUMAN_OVERRIDE_APPROVED": _INFO,
    "HUMAN_REJECTED":          _ERROR,
    "PAYMENT_SCHEDULED":       _SUCCESS,
    "DISCOUNT_EVALUATED":      _WARNING,
    "DOCUMENT_CONFLICT_DETECTED": _WARNING,
    "VENDOR_AUTO_CREATED":     _WARNING,
}


# ---------------------------------------------------------------------------
# Inline SVG icons for pass/fail (no Unicode emoji, no external assets)
# ---------------------------------------------------------------------------

# 12×12 SVG check mark — white stroke on transparent background.
# Embedded directly in the badge span so it renders without any HTTP request.
_SVG_CHECK = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" '
    'viewBox="0 0 12 12" fill="none" style="vertical-align:middle;margin-right:4px">'
    '<polyline points="2,6 5,9 10,3" stroke="#F5F5F5" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '</svg>'
)

# 12×12 SVG cross mark — white stroke on transparent background.
_SVG_CROSS = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" '
    'viewBox="0 0 12 12" fill="none" style="vertical-align:middle;margin-right:4px">'
    '<line x1="2.5" y1="2.5" x2="9.5" y2="9.5" stroke="#F5F5F5" stroke-width="1.8" '
    'stroke-linecap="round"/>'
    '<line x1="9.5" y1="2.5" x2="2.5" y2="9.5" stroke="#F5F5F5" stroke-width="1.8" '
    'stroke-linecap="round"/>'
    '</svg>'
)

_CHECK_STYLES: dict[bool, tuple[str, str, str, str]] = {
    #         bg        fg            svg_icon    label
    True:  (_SUCCESS, _TEXT_ON_DARK, _SVG_CHECK, "Pass"),
    False: (_ERROR,   _TEXT_ON_DARK, _SVG_CROSS, "Fail"),
}


# ---------------------------------------------------------------------------
# Core renderer
# ---------------------------------------------------------------------------

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


def _badge_html_raw(bg: str, fg: str, inner_html: str) -> str:
    """
    Render a pill-shaped badge whose inner content is raw HTML (e.g. contains
    an SVG icon).  The outer span does NOT escape the content.
    """
    return (
        f'<span style="'
        f"background-color:{bg};"
        f"color:{fg};"
        f"padding:3px 10px;"
        f"border-radius:12px;"
        f"font-size:0.85rem;"
        f"font-weight:600;"
        f"display:inline-flex;"
        f"align-items:center;"
        f"margin:2px 0;"
        f'">{inner_html}</span>'
    )


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def outcome_badge(outcome: str) -> str:
    """Return HTML badge for a pipeline outcome string ('STP', 'EXCEPTION', etc.)."""
    bg, fg, label = _OUTCOME_STYLES.get(
        outcome.upper(),
        (_MUTED, _TEXT_ON_DARK, outcome),
    )
    return _badge_html(bg, fg, label)


def discount_badge(recommendation: str) -> str:
    """Return HTML badge for a discount recommendation string."""
    bg, fg, label = _DISCOUNT_STYLES.get(
        recommendation.upper(),
        (_MUTED, _TEXT_ON_DARK, recommendation),
    )
    return _badge_html(bg, fg, label)


def check_badge(passed: bool) -> str:
    """
    Return HTML badge for a boolean pass/fail check.

    Uses an inline SVG check mark (green) or cross (red) instead of Unicode
    emoji for clear visual distinction that is independent of font rendering.
    """
    bg, fg, svg_icon, label = _CHECK_STYLES[passed]
    return _badge_html_raw(bg, fg, f"{svg_icon}{label}")


def reason_badge(reason_code: str) -> str:
    """Return HTML badge for an exception reason code."""
    label = reason_code.replace("_", " ").title()
    return _badge_html(_REASON, _TEXT_ON_DARK, label)


def render_badge(text: str, colour: str | None = None) -> str:
    """
    Render a free-form badge with an optional custom background colour (hex).
    Defaults to the theme-neutral muted colour if none supplied.
    """
    return _badge_html(colour or _MUTED, _TEXT_ON_DARK, text)
