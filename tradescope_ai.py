"""
TradeScope AI — AI-powered proposal generator for residential contractors.

Setup
-----
    pip install streamlit anthropic reportlab

    export ANTHROPIC_API_KEY="sk-ant-..."      # or put it in .streamlit/secrets.toml
    streamlit run tradescope_ai.py

Optional env var / secret:
    TRADESCOPE_MODEL   Override the Claude model (default below).
"""

import html
import io
import json
import os
import re
from datetime import date

import streamlit as st
import streamlit.components.v1 as components
from anthropic import Anthropic, APIError, AuthenticationError, RateLimitError
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DEFAULT_MODEL = "claude-sonnet-5-5"
NOTES_MARKER = "<<<CONTRACTOR_NOTES>>>"

PROJECT_TYPES = [
    "Kitchen Remodel",
    "Bathroom Renovation",
    "Deck Building",
    "Basement Finishing",
    "Roofing",
    "Siding / Exterior",
    "Flooring",
    "Window / Door Replacement",
    "Room Addition",
    "Fence / Landscaping Hardscape",
    "Painting (Interior / Exterior)",
    "Other (describe in notes)",
]

PRICING_FORMATS = [
    "Fixed / Lump Sum",
    "Itemized by Phase",
    "Time & Materials (with not-to-exceed)",
    "Cost-Plus (with markup %)",
    "Price Range Estimate",
]

PAYMENT_SCHEDULES = [
    "30 / 40 / 30",
    "33 / 33 / 34",
    "50 / 50",
    "10 / 40 / 40 / 10",
    "Let TradeScope AI recommend",
]

SYSTEM_PROMPT = f"""You are TradeScope AI, acting simultaneously as (1) a seasoned residential construction \
estimator with 25+ years of field and bidding experience across remodeling, carpentry, plumbing, electrical \
coordination, roofing and exterior work, and (2) a trade-focused legal consultant who drafts contractor-protective \
proposals and contract language for independent contractors.

Your job: turn a contractor's raw, messy notes (often voice-to-text) into a polished, client-ready proposal that \
wins the job AND protects the contractor.

OUTPUT FORMAT — Markdown only, in exactly this structure and order:

# [Project Type] Proposal
**Prepared for:** [client] | **Prepared by:** [business or "Contractor"] | **Date:** [date]
(include license # / state only if provided)

## 1. Project Overview
A short, confident, client-friendly summary (2-4 sentences) plus a bullet list of key project details \
(location if given, size, materials/finish level, goals).

## 2. Scope of Work
Break the work into logical PHASES (e.g., Phase 1: Site Prep & Demolition, Phase 2: Rough-In, Phase 3: Finishes, \
Phase 4: Cleanup & Walkthrough). Use "### Phase N: Name" subheadings with specific bullet points of tasks. \
Use correct trade terminology and realistic sequencing (demo -> structural -> rough plumbing/electrical/HVAC -> \
inspections -> insulation/drywall -> finishes -> punch list). Convert vague notes into specific deliverables. \
Include an "Included Materials & Allowances" sub-list when relevant, noting where the client selects finishes.

## 3. Estimated Timeline
A markdown table with columns: Phase | Estimated Duration. Then a one-line total duration estimate and a note \
that schedule depends on permits, inspections, material lead times, weather (if exterior) and timely client \
decisions.

## 4. Investment & Payment Structure
- State the total investment according to the requested pricing format. NEVER invent a price. If the contractor \
gave a budget, use it. If no number was given, use a clearly marked placeholder like [$ TOTAL] and flag it in notes.
- Provide a milestone payment table with columns: Milestone | % | Amount | Due. Tie milestones to project \
progress (signing, start/rough-in complete, substantial completion, final walkthrough), never to calendar dates alone.
- Include: price valid for 30 days; change orders must be written and signed before extra work begins; allowance \
overages billed to the client; accepted payment methods placeholder; late-payment language placeholder.

## 5. Standard Exclusions & Disclaimers
Write protective, plain-English language. Always cover:
- **Hidden / concealed conditions:** structural damage, dry rot, termite/pest damage, water damage, mold, \
deteriorated framing/subfloor/joists/sheathing, undersized or non-code framing, outdated or unsafe wiring \
(knob-and-tube, aluminum), galvanized/cast-iron plumbing, and failed flashing discovered once walls, floors or \
surfaces are opened. Repair of these is NOT included and will be quoted as a written change order.
- Hazardous materials (asbestos, lead paint, radon, contaminated soil) — testing/abatement excluded.
- Permits, engineering/architect fees, and inspection fees (state who pays: default to "client" unless notes say otherwise).
- Code upgrades required by the building department beyond the described scope.
- Unforeseen site conditions (buried utilities, rock, poor soil, drainage issues, property-line disputes).
- Pre-existing conditions and cosmetic matching (paint, tile, grout, wood grain, flooring dye lots).
- Settling/cracking in existing structure, normal material variation, and damage from client-supplied materials.
- Work not specifically listed in the Scope of Work.
- Delays outside contractor control (weather, supply chain, utility, inspector, client changes).
- Furniture/belongings protection, and normal dust/disturbance acknowledgement.
- Warranty: workmanship warranty placeholder [1 year], manufacturer warranties pass through to the client, no \
warranty on pre-existing conditions or misuse.
- Insurance & licensing statement placeholder.
- Dispute resolution placeholder (written notice and good-faith cure period, then mediation).
- Any state-required notices as bracketed placeholders (e.g., [STATE-REQUIRED 3-DAY RIGHT TO CANCEL NOTICE], \
[MECHANIC'S LIEN NOTICE], [LICENSE # AND STATE-REQUIRED DISCLOSURES]). Tailor to the project type \
(e.g., ledger attachment/footing depth/frost line and permit for decks; waterproofing/substrate for wet areas; \
tear-off decking condition for roofs; load-bearing walls for kitchens).

## 6. Acceptance
A short acceptance paragraph with signature lines:
Client Signature: ______________________  Date: __________
Contractor Signature: __________________  Date: __________

Then on a new line output the exact marker {NOTES_MARKER} followed by a brief section titled \
"Contractor Review Notes (not part of client proposal)" with bullets covering: assumptions you made, \
missing information to confirm before sending, items to verify against local code, potential upsell/risk items, \
and whether the requested deposit might exceed your state's legal deposit limits (many states cap deposits on \
home-improvement contracts — advise checking). Keep this under 10 bullets.

STYLE RULES
- Professional, confident, plain English. No fluff, no emojis, no hype.
- Be specific where the notes are specific; use [BRACKETED PLACEHOLDERS] where information is missing. Never fabricate \
license numbers, addresses, prices, brand names or dates the contractor didn't provide.
- Do not mention that you are an AI inside the client-facing proposal.
- Use only Markdown headings, bullets, bold, and simple pipe tables (no HTML, no code blocks, no emojis).
- Legal language should be protective but fair; it is a draft for the contractor to review with a licensed attorney \
in their state, and you should say so ONLY in the Contractor Review Notes section.
"""


# --------------------------------------------------------------------------- #
# Helpers: API key, Claude call
# --------------------------------------------------------------------------- #

def get_api_key() -> str | None:
    try:
        if "ANTHROPIC_API_KEY" in st.secrets:
            return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY") or st.session_state.get("manual_key")


def get_model() -> str:
    try:
        if "TRADESCOPE_MODEL" in st.secrets:
            return st.secrets["TRADESCOPE_MODEL"]
    except Exception:
        pass
    return os.environ.get("TRADESCOPE_MODEL", DEFAULT_MODEL)


def build_user_message(d: dict) -> str:
    return f"""Create a proposal from the following contractor input.

TODAY'S DATE: {date.today().strftime('%B %d, %Y')}
CLIENT NAME: {d['client']}
PROJECT TYPE: {d['project_type']}
PRICING FORMAT: {d['pricing_format']}
BUDGET / PRICE (as given by contractor): {d['budget'] or 'NOT PROVIDED'}
PREFERRED PAYMENT SCHEDULE: {d['payment']}
BUSINESS NAME: {d['business'] or 'NOT PROVIDED'}
STATE / LICENSE #: {d['state'] or 'NOT PROVIDED'} / {d['license'] or 'NOT PROVIDED'}
PROJECT ADDRESS: {d['address'] or 'NOT PROVIDED'}

RAW PROJECT NOTES (may be unstructured voice-to-text; clean up, organize, and fill in standard trade scope \
that logically belongs with this work, while flagging assumptions in the Contractor Review Notes):
\"\"\"
{d['notes']}
\"\"\"
"""


def stream_proposal(client: Anthropic, model: str, user_msg: str):
    with client.messages.stream(
        model=model,
        max_tokens=6000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    ) as stream:
        for text in stream.text_stream:
            yield text


def split_notes(full_text: str) -> tuple[str, str]:
    if NOTES_MARKER in full_text:
        proposal, notes = full_text.split(NOTES_MARKER, 1)
        return proposal.strip(), notes.strip()
    return full_text.strip(), ""


# --------------------------------------------------------------------------- #
# PDF generation (Markdown -> ReportLab)
# --------------------------------------------------------------------------- #

def _pdf_safe(text: str) -> str:
    """Strip characters the built-in PDF fonts can't render (emoji, etc.)."""
    text = (
        text.replace("\u2192", "->")
        .replace("\u2610", "[ ]")
        .replace("\u2713", "v")
        .replace("\u00a0", " ")
    )
    return text.encode("cp1252", "ignore").decode("cp1252")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", text)
    return text


def markdown_to_pdf(md: str, title: str = "Proposal") -> bytes:
    md = _pdf_safe(md)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.8 * inch,
        rightMargin=0.8 * inch,
        topMargin=0.8 * inch,
        bottomMargin=0.8 * inch,
        title=title,
        author="TradeScope AI",
    )

    base = getSampleStyleSheet()
    navy = colors.HexColor("#1F3A5F")
    accent = colors.HexColor("#D9822B")

    s_body = ParagraphStyle("body", parent=base["Normal"], fontName="Helvetica",
                            fontSize=10, leading=14, spaceAfter=4)
    s_h1 = ParagraphStyle("h1", parent=base["Title"], fontName="Helvetica-Bold",
                          fontSize=20, leading=24, textColor=navy, alignment=TA_CENTER,
                          spaceAfter=6)
    s_h2 = ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold",
                          fontSize=13, leading=16, textColor=navy, spaceBefore=14, spaceAfter=4)
    s_h3 = ParagraphStyle("h3", parent=base["Heading3"], fontName="Helvetica-Bold",
                          fontSize=11, leading=14, textColor=accent, spaceBefore=8, spaceAfter=2)
    s_cell = ParagraphStyle("cell", parent=s_body, fontSize=9, leading=12, spaceAfter=0)
    s_cell_h = ParagraphStyle("cellh", parent=s_cell, fontName="Helvetica-Bold",
                              textColor=colors.white)

    story = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        stripped = raw.strip()

        if not stripped:
            story.append(Spacer(1, 4))
            i += 1
            continue

        # Horizontal rule
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.lightgrey,
                                    spaceBefore=6, spaceAfter=6))
            i += 1
            continue

        # Headings
        m = re.match(r"^(#{1,4})\s+(.*)", stripped)
        if m:
            level, txt = len(m.group(1)), _inline(m.group(2))
            if level == 1:
                story.append(Paragraph(txt, s_h1))
                story.append(HRFlowable(width="100%", thickness=2, color=accent,
                                        spaceBefore=2, spaceAfter=8))
            elif level == 2:
                story.append(Paragraph(txt, s_h2))
                story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey,
                                        spaceBefore=0, spaceAfter=4))
            else:
                story.append(Paragraph(txt, s_h3))
            i += 1
            continue

        # Tables
        if stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = lines[i].strip().strip("|")
                cells = [c.strip() for c in row.split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                ncols = max(len(r) for r in rows)
                data = []
                for r_idx, r in enumerate(rows):
                    r = r + [""] * (ncols - len(r))
                    st_ = s_cell_h if r_idx == 0 else s_cell
                    data.append([Paragraph(_inline(c), st_) for c in r])
                tbl = Table(data, colWidths=[doc.width / ncols] * ncols, repeatRows=1)
                tbl.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), navy),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F5F8")]),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9D1DC")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]))
                story.append(Spacer(1, 4))
                story.append(tbl)
                story.append(Spacer(1, 8))
            continue

        # Bullets (supports simple nesting by indentation)
        m = re.match(r"^(\s*)[-*+]\s+(.*)", raw)
        if m:
            depth = len(m.group(1).replace("\t", "  ")) // 2
            style = ParagraphStyle("bul", parent=s_body, leftIndent=14 + depth * 14,
                                   bulletIndent=2 + depth * 14, spaceAfter=2)
            story.append(Paragraph(_inline(m.group(2)), style,
                                   bulletText="\u2022" if depth == 0 else "-"))
            i += 1
            continue

        # Numbered list
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", raw)
        if m:
            style = ParagraphStyle("num", parent=s_body, leftIndent=18, bulletIndent=0, spaceAfter=2)
            story.append(Paragraph(_inline(m.group(3)), style, bulletText=f"{m.group(2)}."))
            i += 1
            continue

        # Signature lines: keep underscores readable
        story.append(Paragraph(_inline(stripped).replace("_" * 3, "_" * 3), s_body))
        i += 1

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(0.8 * inch, 0.5 * inch, title)
        canvas.drawRightString(letter[0] - 0.8 * inch, 0.5 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #

def copy_button(text: str) -> None:
    """Clipboard button rendered in an iframe (with a fallback for restricted browsers)."""
    payload = json.dumps(text).replace("</", "<\\/")
    components.html(
        f"""
        <button id="copy-btn" style="
            width:100%; padding:0.6rem 1rem; border-radius:0.5rem; border:1px solid #1F3A5F;
            background:#ffffff; color:#1F3A5F; font-size:1rem; font-weight:600; cursor:pointer;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
            Copy to Clipboard
        </button>
        <script>
            const text = {payload};
            const btn = document.getElementById('copy-btn');
            function done(ok) {{
                btn.textContent = ok ? 'Copied!' : 'Copy failed - select text manually';
                setTimeout(() => btn.textContent = 'Copy to Clipboard', 2000);
            }}
            function fallback() {{
                const ta = document.createElement('textarea');
                ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
                document.body.appendChild(ta); ta.select();
                let ok = false;
                try {{ ok = document.execCommand('copy'); }} catch (e) {{}}
                document.body.removeChild(ta); done(ok);
            }}
            btn.addEventListener('click', () => {{
                if (navigator.clipboard && window.isSecureContext) {{
                    navigator.clipboard.writeText(text).then(() => done(true), fallback);
                }} else {{ fallback(); }}
            }});
        </script>
        """,
        height=52,
    )


CSS = """
<style>
    .block-container {max-width: 760px; padding-top: 1.5rem; padding-bottom: 4rem;}
    .brand {font-size: 1.9rem; font-weight: 800; color: #1F3A5F; margin-bottom: 0;}
    .brand span {color: #D9822B;}
    .tagline {color: #667085; margin-top: 0; margin-bottom: 1.2rem;}
    div[data-testid="stFormSubmitButton"] button, .stDownloadButton button {
        width: 100%; font-weight: 700; padding: 0.65rem 1rem;
    }
    .proposal-box {
        background: #ffffff; color: #1b1f24; border: 1px solid #D0D5DD; border-radius: 12px;
        padding: 1.2rem 1.4rem; box-shadow: 0 1px 4px rgba(16,24,40,.06);
    }
    .proposal-box h1 {color:#1F3A5F; font-size:1.6rem; text-align:center;}
    .proposal-box h2 {color:#1F3A5F; font-size:1.2rem; border-bottom:1px solid #E4E7EC; padding-bottom:.2rem;}
    .proposal-box h3 {color:#D9822B; font-size:1.02rem;}
    .proposal-box table {width:100%; font-size:.92rem;}
    .proposal-box th {background:#1F3A5F; color:#fff;}
    @media (max-width: 640px) {.proposal-box {padding: .9rem;}}
</style>
"""


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

def main() -> None:
    st.set_page_config(page_title="TradeScope AI", page_icon="🛠️", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown('<p class="brand">TradeScope <span>AI</span></p>', unsafe_allow_html=True)
    st.markdown('<p class="tagline">Turn rough job-site notes into a client-ready, '
                'contractor-protective proposal.</p>', unsafe_allow_html=True)

    st.session_state.setdefault("proposal", "")
    st.session_state.setdefault("notes", "")
    st.session_state.setdefault("meta", {})

    # API key fallback (only shown if not configured via env / secrets)
    if not get_api_key():
        with st.expander("🔑 Enter your Anthropic API key", expanded=True):
            key = st.text_input("API key", type="password", placeholder="sk-ant-...")
            if key:
                st.session_state["manual_key"] = key
                st.rerun()

    # ----- Input form ----- #
    with st.form("proposal_form"):
        client_name = st.text_input("Client name *", placeholder="e.g., Mark & Linda Johnson")

        c1, c2 = st.columns(2)
        with c1:
            project_type = st.selectbox("Project type *", PROJECT_TYPES)
        with c2:
            pricing_format = st.selectbox("Pricing format", PRICING_FORMATS)

        notes = st.text_area(
            "Project notes / voice-to-text dump *",
            height=220,
            placeholder=(
                "Paste or dictate everything: sizes, materials, what's being demoed, appliance/fixture "
                "choices, client wishes, access issues, anything weird you saw on the walkthrough..."
            ),
            help="On a phone, tap the microphone on your keyboard to dictate. Messy is fine.",
        )

        c3, c4 = st.columns(2)
        with c3:
            budget = st.text_input("Estimated budget / price", placeholder="e.g., $48,500 or $45-52k")
        with c4:
            payment = st.selectbox("Payment milestones", PAYMENT_SCHEDULES)

        with st.expander("Business details (optional, improves the proposal)"):
            business = st.text_input("Business name")
            c5, c6 = st.columns(2)
            with c5:
                state = st.text_input("State", placeholder="e.g., CA")
            with c6:
                license_no = st.text_input("License #")
            address = st.text_input("Project address")

        submitted = st.form_submit_button("⚡ Generate Proposal", type="primary")

    # ----- Generate ----- #
    if submitted:
        if not client_name.strip() or not notes.strip():
            st.error("Please enter at least a client name and some project notes.")
        elif not get_api_key():
            st.error("Add your Anthropic API key above (or set ANTHROPIC_API_KEY) to continue.")
        else:
            data = {
                "client": client_name.strip(),
                "project_type": project_type,
                "pricing_format": pricing_format,
                "budget": budget.strip(),
                "payment": payment,
                "notes": notes.strip(),
                "business": business.strip(),
                "state": state.strip(),
                "license": license_no.strip(),
                "address": address.strip(),
            }
            client = Anthropic(api_key=get_api_key())
            placeholder = st.empty()
            full = ""
            try:
                with st.spinner("Drafting your proposal..."):
                    for chunk in stream_proposal(client, get_model(), build_user_message(data)):
                        full += chunk
                        placeholder.markdown(full.split(NOTES_MARKER)[0] + " ▌")
                placeholder.empty()
                proposal, review_notes = split_notes(full)
                st.session_state["proposal"] = proposal
                st.session_state["notes"] = review_notes
                st.session_state["meta"] = {
                    "client": data["client"],
                    "project_type": data["project_type"],
                }
            except AuthenticationError:
                placeholder.empty()
                st.error("Authentication failed. Check your Anthropic API key.")
            except RateLimitError:
                placeholder.empty()
                st.error("Rate limit reached. Wait a moment and try again.")
            except APIError as e:
                placeholder.empty()
                st.error(f"Anthropic API error: {e}")
            except Exception as e:  # noqa: BLE001
                placeholder.empty()
                st.error(f"Something went wrong: {e}")

    # ----- Output ----- #
    if st.session_state["proposal"]:
        st.divider()
        st.subheader("Your Proposal")

        tab_preview, tab_edit = st.tabs(["Preview", "Edit text"])
        with tab_edit:
            edited = st.text_area(
                "Tweak anything before copying or downloading",
                value=st.session_state["proposal"],
                height=500,
                label_visibility="collapsed",
            )
            st.session_state["proposal"] = edited

        proposal_text = st.session_state["proposal"]

        with tab_preview:
            with st.container(border=True):
                st.markdown(proposal_text)

        meta = st.session_state["meta"]
        safe_client = re.sub(r"[^A-Za-z0-9]+", "_", meta.get("client", "Client")).strip("_")
        safe_type = re.sub(r"[^A-Za-z0-9]+", "_", meta.get("project_type", "Project")).strip("_")
        filename = f"{safe_client}_{safe_type}_Proposal.pdf"

        b1, b2 = st.columns(2)
        with b1:
            copy_button(proposal_text)
        with b2:
            try:
                pdf_bytes = markdown_to_pdf(
                    proposal_text, title=f"{meta.get('project_type', 'Project')} Proposal"
                )
                st.download_button(
                    "⬇️ Download as PDF",
                    data=pdf_bytes,
                    file_name=filename,
                    mime="application/pdf",
                    use_container_width=True,
                )
            except Exception as e:  # noqa: BLE001
                st.error(f"PDF generation failed: {e}")

        if st.session_state["notes"]:
            with st.expander("📝 Contractor Review Notes (private — not in the PDF)", expanded=True):
                st.markdown(st.session_state["notes"])

        st.caption(
            "AI-generated draft. Review all scope, pricing, and legal language, and have a licensed "
            "attorney in your state approve your contract terms before sending."
        )


if __name__ == "__main__":
    main()
