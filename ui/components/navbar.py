"""
ui/components/navbar.py — Fixed pill-shaped top navigation bar.

Architecture decision
---------------------
Streamlit's sidebar (st.radio inside ``with st.sidebar``) cannot be CSS-
transformed into a horizontal top bar — the DOM layout model pins it to the
left rail and its children render as vertical stacks.  Reskinning it to look
like a horizontal pill would require overriding so many structural rules that
it would break on every Streamlit minor release.

The correct approach is:

  1. Hide Streamlit's own header (``[data-testid="stHeader"]``) and collapse
     the sidebar to zero width via the CSS injected from theme.py.
  2. Render a custom ``<nav>`` block via ``st.markdown(..., unsafe_allow_html=True)``
     at position ``fixed; top: 1rem`` so it floats above the content card.
  3. Route via ``st.query_params["page"]`` — setting a query param triggers
     a Streamlit rerun (the script re-executes top-to-bottom), which is
     identical in effect to clicking a radio option.  The param survives the
     browser URL so pages are bookmarkable.

Nav items
---------
The four primary pages are exposed in the pill bar:
  - Dashboard
  - Invoice Processing
  - Audit Log
  - Discount Optimization

System Information is accessible via the settings icon on the right (it's a
utility page, not a primary workflow step).

Active state
------------
The currently active nav item receives an underline in electric blue
(#1E5FD9) and slightly heavier Inter weight.  All other items are navy
(#0D1B3E) at regular weight.  No colour fills, no background pills on items —
just the underline indicator, keeping the overall pill monochrome.

Logo / wordmark
---------------
"LG" lettermark in navy + "LedgerGate" wordmark in Inter 800.  No external
image dependency.  SVG drawn inline so it never makes a network request.

Right-side icon
---------------
A minimal settings gear SVG.  Clicking it sets ``?page=system_info`` so it
navigates to the System Information page.

Click wiring
------------
Each nav link is an ``<a href="?page=KEY">`` anchor.  Streamlit intercepts
anchor-tag navigations that match the current app origin and reruns the
script with the updated query params — no JavaScript required.

Usage
-----
    from ui.components.navbar import render_navbar, get_current_page

    # In streamlit_app.py, after inject_theme():
    page = render_navbar()
    if page == "dashboard":
        dashboard.render()
    ...
"""

from __future__ import annotations

import streamlit as st

# ---------------------------------------------------------------------------
# Page registry — single source of truth for nav items and routing keys
# ---------------------------------------------------------------------------

# (url_key, display_label, show_in_pill_bar)
_PAGES: list[tuple[str, str, bool]] = [
    ("dashboard",             "Dashboard",             True),
    ("invoice_processing",    "Invoice Processing",    True),
    ("audit_log",             "Audit Log",             True),
    ("discount_optimization", "Discount Optimization", True),
    ("system_info",           "System Information",    False),  # icon only
]

_DEFAULT_PAGE = "dashboard"

# ---------------------------------------------------------------------------
# Design tokens (mirrored from theme.py — kept local to avoid circular import)
# ---------------------------------------------------------------------------
_NAVY_DEEP     = "#050B1A"
_NAVY_MID      = "#0D1B3E"
_BLUE_ELECTRIC = "#1E5FD9"
_CARD_SURFACE  = "#FAFBFF"
_NAV_HEIGHT    = "60px"   # pill height — must match padding-top in theme.py

# ---------------------------------------------------------------------------
# SVG assets (inline — zero network requests)
# ---------------------------------------------------------------------------

# Minimal "LG" lettermark: two letters in a rounded square
_LOGO_SVG = (
    '<svg width="32" height="32" viewBox="0 0 32 32" fill="none" '
    'xmlns="http://www.w3.org/2000/svg">'
    # rounded background
    '<rect width="32" height="32" rx="8" fill="#0D1B3E"/>'
    # "L"
    '<text x="5" y="22" font-family="Inter,sans-serif" font-size="14" '
    'font-weight="800" fill="#FFFFFF">L</text>'
    # "G"
    '<text x="16" y="22" font-family="Inter,sans-serif" font-size="14" '
    'font-weight="800" fill="#1E5FD9">G</text>'
    '</svg>'
)

# Settings gear icon (Heroicons outline, 20×20 viewport)
_ICON_SETTINGS = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
    'stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg">'
    '<circle cx="12" cy="12" r="3"/>'
    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06'
    'a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09'
    'A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83'
    'l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09'
    'A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83'
    'l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09'
    'a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83'
    'l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09'
    'a1.65 1.65 0 0 0-1.51 1z"/>'
    '</svg>'
)

# ---------------------------------------------------------------------------
# HTML / CSS template
# ---------------------------------------------------------------------------

def _nav_html(active_key: str) -> str:
    """
    Build the full navbar HTML string for the given active page key.

    The navbar is a fixed ``<nav>`` element rendered via st.markdown.
    It sits above the content card at z-index 1000.

    Structure:
        <nav .lg-nav>
          <div .lg-nav__left>    logo + wordmark
          <div .lg-nav__center>  pill nav items
          <div .lg-nav__right>   settings icon link
    """

    # Build nav item HTML for each primary page
    def _item(key: str, label: str) -> str:
        is_active = key == active_key
        active_class = " lg-nav__item--active" if is_active else ""
        return (
            f'<a href="?page={key}" class="lg-nav__item{active_class}" '
            f'aria-current="{"page" if is_active else "false"}">'
            f'{label}'
            f'</a>'
        )

    items_html = "\n".join(
        _item(key, label)
        for key, label, in_bar in _PAGES
        if in_bar
    )

    settings_active_class = " lg-nav__icon--active" if active_key == "system_info" else ""

    return f"""
<style>
/* ── Navbar shell ────────────────────────────────────────────────────────── */
.lg-nav {{
    position: fixed;
    top: 20px;
    left: 50%;
    transform: translateX(-50%);
    width: min(88vw, 860px);
    height: auto;
    background: {_CARD_SURFACE};
    border-radius: 9999px;
    box-shadow:
        0 8px 32px rgba(5, 11, 26, 0.22),
        0 2px  8px rgba(5, 11, 26, 0.14);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 20px;
    z-index: 1000;
}}

/* ── Left: logo + wordmark ───────────────────────────────────────────────── */
.lg-nav__left {{
    display: flex;
    align-items: center;
    gap: 0.55rem;
    flex-shrink: 0;
    text-decoration: none;
}}

.lg-nav__wordmark {{
    font-family: 'Inter', sans-serif;
    font-weight: 800;
    font-size: 0.95rem;
    color: {_NAVY_MID};
    letter-spacing: -0.02em;
    line-height: 1;
    white-space: nowrap;
    text-decoration: none;
}}

/* ── Center: nav items ───────────────────────────────────────────────────── */
.lg-nav__center {{
    display: flex;
    align-items: center;
    gap: 0.15rem;
    flex: 1;
    justify-content: center;
}}

.lg-nav__item {{
    font-family: 'Inter', sans-serif;
    font-weight: 400;
    font-size: 0.875rem;
    color: {_NAVY_MID};
    text-decoration: none;
    padding: 0.35rem 0.85rem;
    border-radius: 9999px;
    position: relative;
    transition: color 0.15s ease, background 0.15s ease;
    white-space: nowrap;
    opacity: 0.70;
}}

.lg-nav__item:hover {{
    opacity: 1.0;
    background: rgba(13, 27, 62, 0.05);
    color: {_NAVY_MID};
    text-decoration: none;
}}

/* Active indicator: underline dot rendered as ::after pseudo-element */
.lg-nav__item--active {{
    font-weight: 600;
    color: {_NAVY_MID};
    opacity: 1.0;
}}

.lg-nav__item--active::after {{
    content: '';
    position: absolute;
    bottom: 4px;
    left: 50%;
    transform: translateX(-50%);
    width: 20px;
    height: 2.5px;
    background: {_BLUE_ELECTRIC};
    border-radius: 9999px;
}}

/* ── Right: icon button ──────────────────────────────────────────────────── */
.lg-nav__right {{
    flex-shrink: 0;
    display: flex;
    align-items: center;
}}

.lg-nav__icon {{
    display: flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border-radius: 9999px;
    color: {_NAVY_MID};
    opacity: 0.55;
    text-decoration: none;
    transition: opacity 0.15s ease, background 0.15s ease;
}}

.lg-nav__icon:hover {{
    opacity: 1.0;
    background: rgba(13, 27, 62, 0.06);
    text-decoration: none;
}}

.lg-nav__icon--active {{
    opacity: 1.0;
    color: {_BLUE_ELECTRIC};
}}
</style>

<nav class="lg-nav" role="navigation" aria-label="Main navigation">

  <a href="?page={_DEFAULT_PAGE}" class="lg-nav__left" aria-label="LedgerGate home">
    {_LOGO_SVG}
    <span class="lg-nav__wordmark">LedgerGate</span>
  </a>

  <div class="lg-nav__center">
    {items_html}
  </div>

  <div class="lg-nav__right">
    <a href="?page=system_info"
       class="lg-nav__icon{settings_active_class}"
       aria-label="System Information"
       title="System Information">
      {_ICON_SETTINGS}
    </a>
  </div>

</nav>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_current_page() -> str:
    """
    Return the currently active page key from ``st.query_params``.

    Falls back to ``_DEFAULT_PAGE`` if the param is absent or invalid.
    """
    raw = st.query_params.get("page", _DEFAULT_PAGE)
    valid_keys = {key for key, _, _ in _PAGES}
    return raw if raw in valid_keys else _DEFAULT_PAGE


def render_navbar() -> str:
    """
    Render the fixed pill navbar and return the active page key.

    Call this in ``streamlit_app.py`` after ``inject_theme()`` and before
    routing to any page render function.

    Returns
    -------
    str
        The active page key, e.g. ``"dashboard"``, ``"invoice_processing"``.
        Use this to dispatch to the correct page render function.

    Example::

        from ui.components.navbar import render_navbar

        page = render_navbar()
        if page == "dashboard":
            dashboard.render()
        elif page == "invoice_processing":
            invoice_processing.render()
        ...
    """
    active = get_current_page()
    st.markdown(_nav_html(active), unsafe_allow_html=True)
    return active
