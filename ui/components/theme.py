"""
ui/components/theme.py — Centralised typography, gradient background, and
component-surface CSS for LedgerGate-Agent.

Design model
------------
The gradient IS the page surface.  Page titles, section headers, body text,
and empty space all sit directly on the gradient.  No white wrapper wraps the
full content column.

White/light surfaces are applied only to discrete data-bearing components
whose content requires a stable reading background:
  - Individual KPI metric cards
  - Forms
  - Expanders (collapsible detail panels)
  - Dataframes / tables
  - Alert boxes
  - Tab panels

Everything else — headings, descriptive text, dividers, spacing — renders
directly on the gradient with white/light text.

Layering
--------
  [data-testid="stAppViewContainer"]  carries the gradient (145° navy→blue→near-white)
  .block-container                    transparent — gradient shows through
  st.metric / st.form / st.expander   white card surfaces, subtle shadow
  Text on gradient                    white (#FFFFFF) or near-white (#E8EEFF)
  Text on white cards                 navy (#0D1B3E)

Nav bar pill (navbar.py)
------------------------
  position: fixed, top: 20px, margin: 0 auto, white pill, real drop shadow,
  fully rounded — gradient visible on all four sides.

Call inject_theme() once at the top of every page render() before any st.*
content calls.  Re-injection is idempotent.
"""

from __future__ import annotations

import streamlit as st

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------
_NAVY_DEEP      = "#050B1A"
_NAVY_MID       = "#0D1B3E"
_BLUE_ELECTRIC  = "#1E5FD9"
_NEAR_WHITE     = "#F0F4FF"
_CARD_SURFACE   = "#FFFFFF"
_CARD_SHADOW    = "0 2px 12px rgba(5, 11, 26, 0.14), 0 1px 3px rgba(5, 11, 26, 0.08)"
_CARD_BORDER    = "1px solid rgba(30, 95, 217, 0.12)"

# Text on the gradient background
_TEXT_ON_GRAD   = "#FFFFFF"          # page titles, headings on gradient
_TEXT_MUTED     = "rgba(255,255,255,0.75)"   # captions / secondary on gradient

# Text on white card surfaces
_TEXT_ON_CARD   = _NAVY_MID

_TEXT_ON_DARK   = "#FFFFFF"          # inside sidebar

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_THEME_CSS = f"""
<style>
/* ── 0. Google Fonts ─────────────────────────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap');

/* ── 1. Universal font family ────────────────────────────────────────────── */
html, body, [class*="css"],
.stApp, .stMarkdown, .stText, .stCaption,
.stDataFrame, .stMetric, .stSelectbox,
.stTextInput, .stTextArea, .stButton,
.stRadio, .stCheckbox, .stExpander,
.stForm, .stSidebar,
div[data-testid="stSidebar"],
div[data-testid="stAppViewContainer"],
div[data-testid="stVerticalBlock"] {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
}}

/* ── 2. Gradient on the outermost container ──────────────────────────────── */
[data-testid="stAppViewContainer"] {{
    background: linear-gradient(
        145deg,
        {_NAVY_DEEP}     0%,
        {_BLUE_ELECTRIC} 42%,
        {_NEAR_WHITE}    100%
    ) !important;
    background-attachment: fixed !important;
    min-height: 100vh !important;
}}

/* Everything above stAppViewContainer is transparent */
.stApp, body, html {{
    background: transparent !important;
}}

/* Streamlit header strip — transparent so gradient shows through */
[data-testid="stHeader"] {{
    background: transparent !important;
    backdrop-filter: none !important;
}}

/* ── 3. Main content column — TRANSPARENT, gradient shows directly ───────── */
/* This is the key change: no white wrapper around the whole page.            */
.main .block-container,
.block-container,
div[data-testid="stMainBlockContainer"] {{
    background: transparent !important;
    box-shadow: none !important;
    border: none !important;
    border-radius: 0 !important;
    padding-top: 100px !important;
    padding-bottom: 3rem !important;
}}

/* ── 4. Typography on gradient — white / near-white text ────────────────── */

/* Inter 800 — page titles directly on gradient */
h1,
div[data-testid="stHeadingWithActionElements"] h1 {{
    font-weight: 800 !important;
    letter-spacing: -0.02em !important;
    color: {_TEXT_ON_GRAD} !important;
    text-shadow: 0 1px 4px rgba(5, 11, 26, 0.4) !important;
}}

/* Inter 600 — section headers on gradient */
h2, h3,
div[data-testid="stHeadingWithActionElements"] h2,
div[data-testid="stHeadingWithActionElements"] h3 {{
    font-weight: 600 !important;
    letter-spacing: -0.01em !important;
    color: {_TEXT_ON_GRAD} !important;
    text-shadow: 0 1px 3px rgba(5, 11, 26, 0.3) !important;
}}

/* Inter 400 — body text and captions on gradient */
p, li, .stMarkdown p {{
    font-weight: 400 !important;
    color: {_TEXT_ON_GRAD} !important;
}}

.stCaption, small {{
    color: {_TEXT_MUTED} !important;
    font-weight: 400 !important;
}}

/* Dividers on gradient — white with low opacity */
hr {{
    border-color: rgba(255, 255, 255, 0.18) !important;
}}

/* ── 5. KPI metric cards — discrete white surface ────────────────────────── */
div[data-testid="stMetric"] {{
    background: {_CARD_SURFACE} !important;
    border: {_CARD_BORDER} !important;
    border-radius: 10px !important;
    padding: 1rem 1.25rem !important;
    box-shadow: {_CARD_SHADOW} !important;
}}

/* Metric text on white card — navy */
div[data-testid="stMetric"] label,
label[data-testid="stMetricLabel"],
div[data-testid="stMetricValue"],
div[data-testid="stMetricDelta"] {{
    color: {_TEXT_ON_CARD} !important;
    font-weight: 600 !important;
}}

div[data-testid="stMetricValue"] {{
    font-weight: 800 !important;
}}

/* ── 6. Forms — discrete white surface ───────────────────────────────────── */
div[data-testid="stForm"] {{
    background: {_CARD_SURFACE} !important;
    border: {_CARD_BORDER} !important;
    border-radius: 10px !important;
    padding: 1.25rem !important;
    box-shadow: {_CARD_SHADOW} !important;
}}

/* Form interior text — navy on white */
div[data-testid="stForm"] p,
div[data-testid="stForm"] label,
div[data-testid="stForm"] .stMarkdown p {{
    color: {_TEXT_ON_CARD} !important;
    text-shadow: none !important;
}}

div[data-testid="stForm"] h2,
div[data-testid="stForm"] h3 {{
    color: {_TEXT_ON_CARD} !important;
    text-shadow: none !important;
}}

/* ── 7. Expanders — discrete white surface ───────────────────────────────── */
div[data-testid="stExpander"] {{
    background: {_CARD_SURFACE} !important;
    border: {_CARD_BORDER} !important;
    border-radius: 10px !important;
    box-shadow: {_CARD_SHADOW} !important;
}}

div[data-testid="stExpander"] summary p,
div[data-testid="stExpander"] p,
div[data-testid="stExpander"] label,
div[data-testid="stExpander"] h2,
div[data-testid="stExpander"] h3 {{
    color: {_TEXT_ON_CARD} !important;
    text-shadow: none !important;
    font-weight: 600 !important;
}}

div[data-testid="stExpander"] .stMarkdown p {{
    color: {_TEXT_ON_CARD} !important;
    text-shadow: none !important;
    font-weight: 400 !important;
}}

/* ── 8. Dataframes / tables — discrete white surface ─────────────────────── */
div[data-testid="stDataFrame"],
.stDataFrame {{
    background: {_CARD_SURFACE} !important;
    border-radius: 10px !important;
    overflow: hidden !important;
    box-shadow: {_CARD_SHADOW} !important;
    border: {_CARD_BORDER} !important;
}}

/* ── 9. Alert boxes — discrete surface ───────────────────────────────────── */
div[data-testid="stAlert"] {{
    border-radius: 8px !important;
    border-left-width: 4px !important;
}}

/* Alert text — keep Streamlit's semantic colours, just ensure readability */
div[data-testid="stAlert"] p {{
    color: inherit !important;
    text-shadow: none !important;
}}

/* ── 10. Tab panels — discrete white surface ──────────────────────────────── */
div[data-testid="stTabs"] [role="tablist"] {{
    background: rgba(255, 255, 255, 0.15) !important;
    border-bottom: 2px solid rgba(255, 255, 255, 0.25) !important;
    border-radius: 6px 6px 0 0 !important;
    padding: 0 0.5rem !important;
    backdrop-filter: blur(4px) !important;
}}

div[data-testid="stTabs"] [role="tab"] {{
    color: rgba(255, 255, 255, 0.75) !important;
    font-weight: 600 !important;
    padding: 0.6rem 1rem !important;
}}

div[data-testid="stTabs"] [role="tab"][aria-selected="true"] {{
    color: {_TEXT_ON_GRAD} !important;
    border-bottom: 2px solid {_TEXT_ON_GRAD} !important;
    background: transparent !important;
}}

div[data-testid="stTabContent"] {{
    background: {_CARD_SURFACE} !important;
    border: {_CARD_BORDER} !important;
    border-top: none !important;
    border-radius: 0 0 8px 8px !important;
    padding: 1.25rem !important;
    box-shadow: {_CARD_SHADOW} !important;
}}

/* Tab content interior text — navy on white */
div[data-testid="stTabContent"] p,
div[data-testid="stTabContent"] label,
div[data-testid="stTabContent"] .stMarkdown p {{
    color: {_TEXT_ON_CARD} !important;
    text-shadow: none !important;
}}

/* ── 11. Input widgets — white surface, navy text ─────────────────────────── */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea {{
    background: {_CARD_SURFACE} !important;
    border: 1px solid rgba(30, 95, 217, 0.30) !important;
    color: {_TEXT_ON_CARD} !important;
    border-radius: 6px !important;
}}

.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus {{
    border-color: {_BLUE_ELECTRIC} !important;
    box-shadow: 0 0 0 3px rgba(30, 95, 217, 0.20) !important;
    outline: none !important;
}}

/* Input labels on gradient — white */
.stTextInput label,
.stTextArea label,
.stSelectbox label,
.stNumberInput label,
.stDateInput label,
.stRadio label,
.stCheckbox label,
.stFileUploader label {{
    color: {_TEXT_ON_GRAD} !important;
    text-shadow: none !important;
}}

/* Selectbox / number input interior */
div[data-testid="stSelectbox"] > div,
div[data-testid="stNumberInput"] input {{
    background: {_CARD_SURFACE} !important;
    color: {_TEXT_ON_CARD} !important;
    border-radius: 6px !important;
}}

/* ── 12. Sidebar — deep navy gradient rail ────────────────────────────────── */
div[data-testid="stSidebar"],
section[data-testid="stSidebar"] {{
    background: linear-gradient(
        180deg,
        {_NAVY_DEEP} 0%,
        {_NAVY_MID}  100%
    ) !important;
    border-right: 1px solid rgba(30, 95, 217, 0.20) !important;
}}

div[data-testid="stSidebar"] *,
section[data-testid="stSidebar"] * {{
    color: {_TEXT_ON_DARK} !important;
}}

div[data-testid="stSidebar"] hr {{
    border-color: rgba(255, 255, 255, 0.15) !important;
}}

div[data-testid="stSidebar"] .stCaption,
div[data-testid="stSidebar"] small {{
    color: rgba(255, 255, 255, 0.60) !important;
}}

/* ── 13. Scrollbar ────────────────────────────────────────────────────────── */
::-webkit-scrollbar {{ width: 6px; height: 6px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{
    background: rgba(255, 255, 255, 0.25);
    border-radius: 3px;
}}
::-webkit-scrollbar-thumb:hover {{
    background: rgba(255, 255, 255, 0.50);
}}
</style>
"""


def inject_theme() -> None:
    """
    Inject the LedgerGate-Agent theme CSS.

    The gradient is the page surface.  Page titles, headings, body text,
    and empty space render directly on the gradient with white text.
    White card surfaces are applied only to discrete data components:
    metric cards, forms, expanders, dataframes, alert boxes, tab panels.

    Call once at the top of every page render() function.
    """
    st.markdown(_THEME_CSS, unsafe_allow_html=True)
