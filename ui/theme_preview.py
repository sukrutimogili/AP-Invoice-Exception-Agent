"""
ui/theme_preview.py — LedgerGate-Agent theme preview (dev tool).

NOT a production page.  Run this standalone to check the full palette in
isolation before trusting it across all five pages.

Launch:
    streamlit run ui/theme_preview.py

Renders one of every themed Streamlit component and every badge variant so
you can verify:
  - Gradient background (deep navy → electric blue → near-white) is visible
    in the viewport margins and behind the sidebar, NOT behind card content
  - Card surfaces (#FAFBFF) render with soft shadow, clearly floating above
    the gradient
  - Hero band (navy→blue gradient) appears behind each page h1 with white text
  - primaryColor (#1E5FD9 electric blue) appears on interactive widgets
    (button, slider, radio, checkbox focus ring) — NOT as body text
  - textColor (#0D1B3E dark navy) is readable against card surface (#FAFBFF)
  - Error/warning/success states are visually distinct from each other
  - Badge pills have sufficient contrast on the card surface
  - Inter font is applied at the correct weight tiers: 800 h1, 600 h2/h3, 400 body
  - Sidebar is deep-navy with white text

Contrast reference (WCAG, verified):
  #0D1B3E on #FAFBFF  →  16.3:1  AAA  (body text on card)
  #0D1B3E on #F0F4FF  →  15.4:1  AAA  (body text on page bg)
  #FFFFFF on #1E5FD9  →   5.7:1  AA   (white on electric blue — hero/buttons)
  #FFFFFF on #050B1A  →  19.6:1  AAA  (white on deep navy — sidebar)
"""

from __future__ import annotations

import streamlit as st

# Streamlit requires set_page_config as the very first call when running standalone.
st.set_page_config(
    page_title="Theme Preview — LedgerGate-Agent",
    page_icon=":art:",
    layout="wide",
)

# Inject shared Inter font + gradient + card-surface CSS — same call as every
# production page.  This must come before any st.* content calls.
from ui.components.theme import inject_theme  # noqa: E402
inject_theme()

# Import badge helpers after page config
from ui.components.badges import (  # noqa: E402
    AUDIT_EVENT_COLOURS,
    check_badge,
    discount_badge,
    outcome_badge,
    reason_badge,
    render_badge,
)
from models.enums import ExceptionReasonCode  # noqa: E402

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("LedgerGate-Agent — Theme Preview")
st.caption(
    "Dev tool only — not a production page.  "
    "New palette: `#050B1A` deep navy · `#1E5FD9` electric blue · "
    "`#F0F4FF` near-white · `#FAFBFF` card surface · `#0D1B3E` text"
)
st.divider()

# ---------------------------------------------------------------------------
# 1. KPI Metrics (st.metric)
# ---------------------------------------------------------------------------
st.subheader("1 · KPI Metrics  `st.metric`")
st.caption(
    "Verify: metric cards sit on #FAFBFF with soft shadow.  "
    "Value text (#0D1B3E navy) is readable.  "
    "Delta colours (positive = green, negative = red) are distinct."
)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("STP Rate", "82.4%", help="Percentage of invoices straight-through processed")
c2.metric("STP / Exception", "47 / 10")
c3.metric("Open Exceptions", 3, delta="+2", delta_color="inverse")
c4.metric("Discount Captured", "$1,240.00", delta="+$320", delta_color="normal")
c5.metric("Vendor Auto-Creates", 2, delta="+2", delta_color="inverse",
          help="Non-zero = needs attention")

st.divider()

# ---------------------------------------------------------------------------
# 2. Interactive widgets — primaryColor should appear here
# ---------------------------------------------------------------------------
st.subheader("2 · Interactive Widgets  (primaryColor = electric blue #1E5FD9)")
st.caption(
    "Electric blue accent should appear on: button fill (primary), slider handle, "
    "radio selection dot, checkbox tick, text-input focus ring.  "
    "NOT on body text or background fills."
)

col_a, col_b, col_c = st.columns(3)

with col_a:
    st.button("Primary button", type="primary")
    st.button("Secondary button", type="secondary")

with col_b:
    st.slider("Slider (blue handle)", 0, 100, 40)
    st.checkbox("Checkbox (blue tick)")

with col_c:
    st.radio("Radio (blue dot)", ["Option A", "Option B", "Option C"], index=0)
    st.selectbox("Selectbox", ["Choice 1", "Choice 2"])

st.text_input("Text input (blue focus ring)", placeholder="Click to see focus colour…")

st.divider()

# ---------------------------------------------------------------------------
# 3. Status components — must be visually distinct from each other
# ---------------------------------------------------------------------------
st.subheader("3 · Status Components  (success / warning / error / info)")
st.caption(
    "Critical check: success (green), warning (amber), error (red), info (blue) "
    "must all be distinguishable at a glance.  "
    "Electric blue (#1E5FD9) must NOT appear on these — it is reserved for interactive "
    "elements only, so the success/warning semantic distinction stays clear."
)

st.success("STP Approved — invoice passed all FR-3.1 checks and is scheduled for payment.")
st.warning("Low-Confidence Extraction — one or more fields flagged uncertain; human review required.")
st.error("Extraction Failed — invoice could not be extracted after two attempts (NEEDS_REEXTRACTION).")
st.info("No open exceptions — the exception queue is currently clear.")

st.divider()

# ---------------------------------------------------------------------------
# 4. Expander (card surface)
# ---------------------------------------------------------------------------
st.subheader("4 · Expander  (card surface #FAFBFF with border)")
st.caption(
    "Expander interior should sit on #FAFBFF with a faint border and soft shadow.  "
    "It must be visually distinct from the gradient visible in the page margins."
)

with st.expander("Sample Invoice Fields", expanded=True):
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Invoice #:** INV-2026-PREVIEW")
        st.markdown("**Vendor:** Acme Supplies Ltd")
        st.markdown("**Invoice Date:** 2026-03-01")
        st.markdown("**Due Date:** 2026-04-01")
    with col2:
        st.markdown("**PO Reference:** PO-2026-001")
        st.markdown("**Contract Ref:** CTR-2026-001")
        st.markdown("**Payment Terms:** Net 30")
        st.markdown("**Grand Total:** $1,100.00")

with st.expander("Exception Details (collapsed by default)", expanded=False):
    st.markdown("This expander is collapsed — open it to check the interior colour.")
    st.error("Sample error state inside an expander.")

st.divider()

# ---------------------------------------------------------------------------
# 5. Outcome badges
# ---------------------------------------------------------------------------
st.subheader("5 · Outcome Badges  (inline HTML pills)")
st.caption(
    "Each pill uses a semantic dark background with white text — self-contained, "
    "readable on any surface.  Verify contrast of white text on each pill bg."
)

outcomes = ["STP", "EXCEPTION", "NEEDS_REEXTRACTION", "APPROVED", "REJECTED", "ERROR"]
badge_html = "  ".join(outcome_badge(o) for o in outcomes)
st.markdown(badge_html, unsafe_allow_html=True)

st.divider()

# ---------------------------------------------------------------------------
# 6. Discount badges
# ---------------------------------------------------------------------------
st.subheader("6 · Discount Badges")

disc_options = ["TAKE_DISCOUNT", "HOLD_TO_NET", "WINDOW_MISSED", "NO_DISCOUNT"]
disc_html = "  ".join(discount_badge(d) for d in disc_options)
st.markdown(disc_html, unsafe_allow_html=True)

st.divider()

# ---------------------------------------------------------------------------
# 7. Check badges (Pass / Fail — SVG icons, no emoji)
# ---------------------------------------------------------------------------
st.subheader("7 · Match-Check Badges  (SVG check / cross — no emoji)")
st.caption(
    "Pass = green pill with SVG check mark.  Fail = red pill with SVG cross.  "
    "Both must be distinct from each other AND from electric blue (#1E5FD9) "
    "so a quick scan yields the right reading without colour confusion."
)

checks = {
    "Vendor Known": True,
    "PO Resolved": True,
    "Contract Resolved": False,
    "Quantities Match": True,
    "Prices Match": False,
    "Total Matches": True,
    "Approval Satisfied": True,
}
cols = st.columns(4)
for i, (name, passed) in enumerate(checks.items()):
    with cols[i % 4]:
        st.markdown(
            f"**{name}**  \n{check_badge(passed)}",
            unsafe_allow_html=True,
        )

st.divider()

# ---------------------------------------------------------------------------
# 8. Reason code badges
# ---------------------------------------------------------------------------
st.subheader("8 · Exception Reason Code Badges")

reason_html = "<br>".join(
    reason_badge(rc.value) for rc in ExceptionReasonCode
)
st.markdown(reason_html, unsafe_allow_html=True)

st.divider()

# ---------------------------------------------------------------------------
# 9. Audit event colour palette
# ---------------------------------------------------------------------------
st.subheader("9 · Audit Event Badge Palette")
st.caption("Sourced from AUDIT_EVENT_COLOURS in badges.py — used by Audit Log and Dashboard pages.")

event_html = "  ".join(
    render_badge(evt.replace("_", " ").title(), colour)
    for evt, colour in AUDIT_EVENT_COLOURS.items()
)
st.markdown(event_html, unsafe_allow_html=True)

st.divider()

# ---------------------------------------------------------------------------
# 10. Bar chart colour check
# ---------------------------------------------------------------------------
st.subheader("10 · Bar Charts  (native st.bar_chart)")
st.caption(
    "STP-dominant scenario = green bars.  "
    "Exception-dominant scenario = red bars.  "
    "Verify neither bleeds into electric blue territory."
)

import pandas as pd  # noqa: E402

col_left, col_right = st.columns(2)

with col_left:
    st.markdown("**STP-dominant (green)**")
    df_stp = pd.DataFrame({"Count": [14, 3]}, index=["STP Approved", "Exception Raised"])
    st.bar_chart(df_stp, color="#1A6B2A")

with col_right:
    st.markdown("**Exception-dominant (red)**")
    df_exc = pd.DataFrame({"Count": [3, 11]}, index=["STP Approved", "Exception Raised"])
    st.bar_chart(df_exc, color="#C0392B")

st.divider()

# ---------------------------------------------------------------------------
# 11. Data table
# ---------------------------------------------------------------------------
st.subheader("11 · Data Table  `st.dataframe`")
st.caption(
    "Table should sit on the #FAFBFF card surface with a faint shadow.  "
    "Header row uses the card's secondary background.  "
    "Body text (#0D1B3E navy) must be legible on every row."
)

sample_data = pd.DataFrame({
    "Invoice #": ["INV-001", "INV-002", "INV-003"],
    "Outcome": ["STP", "EXCEPTION", "NEEDS_REEXTRACTION"],
    "Amount": ["$1,100.00", "$4,200.00", "(failed)"],
    "Reason": ["—", "PRICE_VARIANCE", "SCHEMA_VALIDATION_FAILED"],
})
st.dataframe(sample_data, use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# 12. Typography weight tiers
# ---------------------------------------------------------------------------
st.subheader("12 · Typography Weight Tiers  (Inter via Google Fonts)")
st.caption(
    "h1 = Inter 800 extra-bold, white on hero band  ·  "
    "h2/h3 = Inter 600 semi-bold, #0D1B3E on card  ·  "
    "body/table = Inter 400 regular, #0D1B3E on card"
)

st.markdown("## Section Header — Inter 600 (h2)")
st.markdown("### Sub-section — Inter 600 (h3)")
st.markdown(
    "Body paragraph at Inter 400, colour #0D1B3E on card surface #FAFBFF.  "
    "All invoices are validated against a strict schema before any matching runs.  "
    "Grand totals, quantities, and unit prices are compared deterministically — "
    "the LLM is not involved in any arithmetic."
)
st.caption("Caption text — Inter 400, subdued size, #0D1B3E at reduced opacity.")

st.divider()

# ---------------------------------------------------------------------------
# 13. Sidebar check
# ---------------------------------------------------------------------------
st.subheader("13 · Sidebar")
st.caption(
    "Sidebar should be deep-navy gradient (#050B1A → #0D1B3E), white text, "
    "with a faint electric-blue right border.  "
    "It should look like the left edge of the page gradient used as a nav rail."
)

st.divider()

# ---------------------------------------------------------------------------
# Footer — contrast summary for the new palette
# ---------------------------------------------------------------------------
st.caption(
    "**Contrast summary (new palette):**  "
    "`#0D1B3E` on `#FAFBFF` = 16.3:1 AAA  ·  "
    "`#0D1B3E` on `#F0F4FF` = 15.4:1 AAA  ·  "
    "`#FFFFFF` on `#1E5FD9` = 5.7:1 AA  ·  "
    "`#FFFFFF` on `#050B1A` = 19.6:1 AAA  "
    "(all exceed WCAG AA 4.5:1)"
)
