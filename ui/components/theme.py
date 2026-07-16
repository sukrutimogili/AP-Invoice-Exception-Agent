"""
ui/components/theme.py — Centralised typography, gradient background, and
component-surface CSS for LedgerGate-Agent.

Design model
------------
The gradient IS the page surface.  Page titles, section headers, body text,
and empty space all sit directly on the gradient with white/light text.
No white wrapper wraps the full content column.

Gradient: 135° dark navy → mid navy → royal blue → electric blue.
Stays dark throughout — no light or near-white corner anywhere.
  #04060F  0%   — near-black navy (top-left)
  #0B1A3E  35%  — deep navy
  #14357A  65%  — mid royal blue
  #1E5FD9  100% — electric blue (bottom-right)

Typography tiers:
  Outfit 800   — page titles only           (h1 / st.title)
  Outfit 700   — section/card headers       (h2, h3 / st.subheader)
  Inter  400   — all body text, captions    (p, li, labels, captions)
  Inter  600   — inline emphasis / KPI labels inside cards

White/light surfaces applied only to discrete data components:
  KPI metric cards, forms, expanders, dataframes, alert boxes, tab panels.
  Interior text on those cards: Outfit headings → navy, Inter body → navy.

Everything else (headings, descriptions, dividers, empty space) renders
directly on the gradient with white text.

Call inject_theme() once at the top of every page render() before any st.*
content calls.  Re-injection is idempotent.
"""

from __future__ import annotations

import streamlit as st

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------
# Gradient stops — 135°, all dark, no light corner
_GRAD_0   = "#04060F"   # near-black navy    0%
_GRAD_35  = "#0B1A3E"   # deep navy         35%
_GRAD_65  = "#14357A"   # royal blue        65%
_GRAD_100 = "#1E5FD9"   # electric blue    100%

_NAVY_DEEP      = "#050B1A"   # sidebar darkest stop
_NAVY_MID       = "#0D1B3E"   # sidebar lighter stop / card text
_BLUE_ELECTRIC  = "#1E5FD9"   # primary accent (buttons, focus rings)

_CARD_SURFACE   = "#FFFFFF"
_CARD_SHADOW    = "0 2px 16px rgba(4, 6, 15, 0.28), 0 1px 4px rgba(4, 6, 15, 0.16)"
_CARD_BORDER    = "1px solid rgba(30, 95, 217, 0.14)"

# Text on the gradient (dark background everywhere)
_TEXT_ON_GRAD   = "#FFFFFF"
_TEXT_MUTED     = "rgba(255, 255, 255, 0.68)"

# Text on white card surfaces
_TEXT_ON_CARD   = _NAVY_MID
_TEXT_ON_DARK   = "#FFFFFF"   # sidebar

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_THEME_CSS = f"""
<style>
/* ── 0. Google Fonts ─────────────────────────────────────────────────────── */
/* Outfit for headings (700/800), Inter for body (400/600)                   */
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@700;800&family=Inter:wght@400;600&display=swap');

/* ── 1. Universal font family — Inter for everything by default ──────────── */
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
    font-weight: 400;
}}

/* ── 2. Gradient on the outermost container ──────────────────────────────── */
/* 4-stop gradient stays in dark navy-to-royal-blue range throughout —       */
/* no light/near-white corner anywhere on the page.                          */
[data-testid="stAppViewContainer"] {{
    background: linear-gradient(
        135deg,
        #04060F  0%,
        #0B1A3E  35%,
        #14357A  65%,
        #1E5FD9  100%
    ) !important;
    background-attachment: fixed !important;
    /* No min-height: 100vh — container sizes to content, no dead space */
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

/* ── 4. Typography — font families and weights (no colour here) ──────────── */
/* Colours are set per-surface below (gradient context vs card context).     */
/* Setting color globally with !important is what caused white-on-white.     */

/* Outfit 800 — page titles */
h1,
div[data-testid="stHeadingWithActionElements"] h1 {{
    font-family: 'Outfit', sans-serif !important;
    font-weight: 800 !important;
    letter-spacing: -0.02em !important;
}}

/* Outfit 700 — section headers */
h2, h3,
div[data-testid="stHeadingWithActionElements"] h2,
div[data-testid="stHeadingWithActionElements"] h3 {{
    font-family: 'Outfit', sans-serif !important;
    font-weight: 700 !important;
    letter-spacing: -0.01em !important;
}}

/* Inter 400 — body, captions */
p, li, .stMarkdown p {{
    font-family: 'Inter', sans-serif !important;
    font-weight: 400 !important;
}}

.stCaption, small {{
    font-family: 'Inter', sans-serif !important;
    font-weight: 400 !important;
}}

/* ── 4b. Gradient context — white text on dark background ────────────────── */
/* Scoped to the transparent column that sits directly on the gradient.      */
/* Does NOT apply inside stMetric / stForm / stExpander / stTabContent /     */
/* stDataFrame — those are white cards and get navy text below.              */
.block-container > div > div > div > p,
.block-container > div > div > div > li,
.block-container .stMarkdown:not([data-testid="stExpander"] .stMarkdown):not([data-testid="stForm"] .stMarkdown):not([data-testid="stTabContent"] .stMarkdown) p {{
    color: {_TEXT_ON_GRAD} !important;
}}

/* Headings directly on gradient (not inside a card container) */
.block-container > div > div > div h1,
.block-container > div > div > div h2,
.block-container > div > div > div h3 {{
    color: {_TEXT_ON_GRAD} !important;
    text-shadow: 0 1px 4px rgba(4, 6, 15, 0.45) !important;
}}

/* Captions and small text on gradient */
.block-container .stCaption:not([data-testid="stExpander"] .stCaption):not([data-testid="stForm"] .stCaption) {{
    color: {_TEXT_MUTED} !important;
}}

/* Dividers on gradient */
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

div[data-testid="stMetric"] *,
div[data-testid="stMetricLabel"],
div[data-testid="stMetricValue"],
div[data-testid="stMetricDelta"],
label[data-testid="stMetricLabel"] {{
    color: {_TEXT_ON_CARD} !important;
    text-shadow: none !important;
}}

div[data-testid="stMetricLabel"] {{ font-weight: 600 !important; font-family: 'Inter', sans-serif !important; }}
div[data-testid="stMetricValue"] {{ font-weight: 800 !important; font-family: 'Inter', sans-serif !important; }}

/* ── 6. Forms — discrete white surface ───────────────────────────────────── */
div[data-testid="stForm"] {{
    background: {_CARD_SURFACE} !important;
    border: {_CARD_BORDER} !important;
    border-radius: 10px !important;
    padding: 1.25rem !important;
    box-shadow: {_CARD_SHADOW} !important;
}}

div[data-testid="stForm"] *:not(input):not(textarea):not(button):not(select) {{
    color: {_TEXT_ON_CARD} !important;
    text-shadow: none !important;
}}

div[data-testid="stForm"] h1, div[data-testid="stForm"] h2, div[data-testid="stForm"] h3 {{
    font-family: 'Outfit', sans-serif !important;
}}

/* ── 7. Expanders — discrete white surface ───────────────────────────────── */
div[data-testid="stExpander"] {{
    background: {_CARD_SURFACE} !important;
    border: {_CARD_BORDER} !important;
    border-radius: 10px !important;
    box-shadow: {_CARD_SHADOW} !important;
}}

div[data-testid="stExpander"] *:not(input):not(textarea):not(button):not(select) {{
    color: {_TEXT_ON_CARD} !important;
    text-shadow: none !important;
}}

div[data-testid="stExpander"] summary p,
div[data-testid="stExpander"] h2,
div[data-testid="stExpander"] h3 {{
    font-family: 'Outfit', sans-serif !important;
    font-weight: 700 !important;
}}

div[data-testid="stExpander"] p,
div[data-testid="stExpander"] li,
div[data-testid="stExpander"] td,
div[data-testid="stExpander"] th {{
    font-family: 'Inter', sans-serif !important;
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

/* ── 9. Alert boxes — readable text regardless of background ─────────────── */
div[data-testid="stAlert"] {{
    border-radius: 8px !important;
    border-left-width: 4px !important;
}}

/* Override global white rule — Streamlit's alert colouring handles fg */
div[data-testid="stAlert"] p,
div[data-testid="stAlert"] span,
div[data-testid="stAlert"] div {{
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
    color: rgba(255, 255, 255, 0.80) !important;
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

div[data-testid="stTabContent"] *:not(input):not(textarea):not(button):not(select) {{
    color: {_TEXT_ON_CARD} !important;
    text-shadow: none !important;
}}

div[data-testid="stTabContent"] h1,
div[data-testid="stTabContent"] h2,
div[data-testid="stTabContent"] h3 {{
    font-family: 'Outfit', sans-serif !important;
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
.stFileUploader label,
.stSlider label {{
    color: {_TEXT_ON_GRAD} !important;
    text-shadow: none !important;
}}

/* Selectbox — all child text (selected value renders as span/div, not p) */
div[data-testid="stSelectbox"],
div[data-testid="stSelectbox"] > div,
div[data-testid="stSelectbox"] > div > div,
div[data-testid="stSelectbox"] span,
div[data-testid="stSelectbox"] input {{
    background: {_CARD_SURFACE} !important;
    color: {_TEXT_ON_CARD} !important;
    border-radius: 6px !important;
}}

/* Number input */
div[data-testid="stNumberInput"] input,
div[data-testid="stNumberInput"] span {{
    background: {_CARD_SURFACE} !important;
    color: {_TEXT_ON_CARD} !important;
    border-radius: 6px !important;
}}

/* Date input */
div[data-testid="stDateInput"] input {{
    background: {_CARD_SURFACE} !important;
    color: {_TEXT_ON_CARD} !important;
}}

/* Radio and checkbox option text — white on gradient */
div[data-testid="stRadio"] span,
div[data-testid="stCheckbox"] span {{
    color: {_TEXT_ON_GRAD} !important;
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
