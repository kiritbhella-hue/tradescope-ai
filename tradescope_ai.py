"""
TradeScope AI — proposal and contract generator for residential contractors.

Setup
-----
    pip install streamlit>=1.40 anthropic reportlab pillow

    export ANTHROPIC_API_KEY="sk-ant-..."        # or .streamlit/secrets.toml
    streamlit run tradescope_ai.py

Recommended .streamlit/config.toml (the UI is designed for the light theme):

    [theme]
    base = "light"
    primaryColor = "#1E4E8C"

Optional secrets / environment variables
----------------------------------------
    TRADESCOPE_MODEL        Claude model override (default below)
    DEMO_BILLING            "true" (default) shows a one-click demo upgrade; set "false" in production
    STRIPE_PAYMENT_LINK     Your Stripe Payment Link; shows a "Subscribe" button
    PRO_ACTIVATION_CODES    Comma-separated codes that unlock Pro (e.g. issued after checkout)
    TRADESCOPE_DATA_DIR     Where accounts.json is stored (default ./.tradescope_data)

About the subscription system
-----------------------------
Accounts are keyed by the work email typed into the sidebar and stored in a local JSON file.
That is a *simulation* meant for demos and early pilots: anyone can type any email, and
hosts like Streamlit Community Cloud wipe local files on restart. Before charging real
customers, replace `get_account` / `update_account` with a real database, use real sign-in
(st.login / OIDC), and verify subscriptions through Stripe webhooks.
"""

import base64
import hashlib
import html
import io
import json
import os
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from anthropic import Anthropic, APIError, AuthenticationError, RateLimitError
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    HRFlowable,
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# =========================================================================== #
# Configuration
# =========================================================================== #

APP_NAME = "TradeScope AI"
DEFAULT_MODEL = "claude-sonnet-5-5"
NOTES_MARKER = "<<<CONTRACTOR_NOTES>>>"
PAYMENT_MARKER = "[[PAYMENT_TABLE]]"

FREE_TRIAL_LIMIT = 3
PRO_PRICE_LABEL = "$49 per month"
DATA_DIR = Path(os.environ.get("TRADESCOPE_DATA_DIR", ".tradescope_data"))
ACCOUNTS_FILE = DATA_DIR / "accounts.json"

DEFAULT_BRAND = "#1E4E8C"
INK = "#0F1B2D"
STEEL = "#5A6778"
RULE = "#D8DDE5"

PROFILE_FIELDS = ("company", "phone", "email", "license", "state", "address", "website", "color")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

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

AI_RECOMMEND = "Let TradeScope AI recommend"

# Each schedule: list of (percent, milestone, due-when)
SCHEDULES = {
    "30 / 40 / 30": [
        (30, "Deposit: contract signing", "Due at signing, before materials are ordered"),
        (40, "Progress: rough-in complete", "Due when rough-in is complete (and passed inspection where required)"),
        (30, "Final: substantial completion", "Due at final walkthrough on substantial completion"),
    ],
    "33 / 33 / 34": [
        (33, "Deposit: contract signing", "Due at signing, before materials are ordered"),
        (33, "Progress: project midpoint", "Due when the project reaches its midpoint (rough-in complete)"),
        (34, "Final: substantial completion", "Due at final walkthrough on substantial completion"),
    ],
    "50 / 50": [
        (50, "Deposit: contract signing", "Due at signing, before materials are ordered"),
        (50, "Final: substantial completion", "Due at final walkthrough on substantial completion"),
    ],
    "10 / 40 / 40 / 10": [
        (10, "Deposit: contract signing", "Due at signing"),
        (40, "Materials ordered and work started", "Due when materials are ordered and work begins"),
        (40, "Progress: rough-in complete", "Due when rough-in is complete (and passed inspection where required)"),
        (10, "Final: punch list complete", "Due at final walkthrough after punch-list items are finished"),
    ],
}
PAYMENT_SCHEDULES = list(SCHEDULES.keys()) + [AI_RECOMMEND]

SYSTEM_PROMPT = f"""You are TradeScope AI, acting simultaneously as (1) a seasoned residential construction \
estimator with 25+ years of field and bidding experience across remodeling, carpentry, plumbing, electrical \
coordination, roofing and exterior work, and (2) a trade-focused legal consultant who drafts contractor-protective \
proposals and contract language for independent contractors.

Your job: turn a contractor's raw, messy notes (often voice-to-text) into a polished, client-ready proposal and \
contract that wins the job AND protects the contractor.

OUTPUT FORMAT — Markdown only, in exactly this structure and order:

# [Project Type] Proposal
**Prepared for:** [client] | **Prepared by:** [business or "Contractor"] | **Date:** [date]

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
- Follow the PAYMENT TABLE INSTRUCTION in the user message exactly.
- Tie milestones to project progress, never to calendar dates alone.
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
One short paragraph stating that signing below authorizes the work and accepts the scope, price, payment \
schedule, exclusions and terms above. Do NOT write signature lines, underscores, or date blanks — the document \
template adds the signature block automatically.

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


# =========================================================================== #
# Secrets, API access
# =========================================================================== #

def get_secret(name: str, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


def get_api_key() -> str | None:
    return get_secret("ANTHROPIC_API_KEY") or st.session_state.get("manual_key") or None


def get_model() -> str:
    return str(get_secret("TRADESCOPE_MODEL", DEFAULT_MODEL))


def demo_billing_enabled() -> bool:
    return str(get_secret("DEMO_BILLING", "true")).strip().lower() in ("1", "true", "yes", "on")


def valid_codes() -> set[str]:
    raw = get_secret("PRO_ACTIVATION_CODES", "")
    items = raw if (hasattr(raw, "__iter__") and not isinstance(raw, str)) else str(raw).split(",")
    return {str(c).strip().upper() for c in items if str(c).strip()}


# =========================================================================== #
# Accounts, trial and subscription (simulated; see module docstring)
# =========================================================================== #

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _default_account() -> dict:
    return {"plan": "free", "used": 0, "created": _now(), "history": [], "profile": {}}


def _load_db() -> dict:
    try:
        return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_db(db: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2), encoding="utf-8")
    os.replace(tmp, ACCOUNTS_FILE)


def account_id(email: str) -> str | None:
    e = (email or "").strip().lower()
    return e if EMAIL_RE.match(e) else None


def get_account(aid: str) -> dict:
    with _LOCK:
        db = _load_db()
        if aid not in db:
            db[aid] = _default_account()
            _save_db(db)
        return db[aid]


def update_account(aid: str, fn) -> dict:
    with _LOCK:
        db = _load_db()
        acc = db.setdefault(aid, _default_account())
        fn(acc)
        _save_db(db)
        return acc


def trial_remaining(acc: dict) -> int:
    return max(0, FREE_TRIAL_LIMIT - int(acc.get("used", 0)))


def can_generate(acc: dict | None) -> bool:
    if not acc:
        return False
    return acc.get("plan") == "pro" or trial_remaining(acc) > 0


def record_generation(aid: str, meta: dict) -> None:
    def _apply(acc: dict) -> None:
        acc["used"] = int(acc.get("used", 0)) + 1
        acc.setdefault("history", []).append(
            {"ts": _now(), "client": meta.get("client", ""), "project": meta.get("project_type", "")}
        )
        acc["history"] = acc["history"][-100:]

    update_account(aid, _apply)


def set_plan(aid: str, plan: str) -> None:
    update_account(aid, lambda a: a.__setitem__("plan", plan))


# =========================================================================== #
# Branding profile helpers
# =========================================================================== #

def safe_hex(value: str | None) -> str:
    return value if value and re.fullmatch(r"#[0-9a-fA-F]{6}", value) else DEFAULT_BRAND


def luminance(hex_color: str) -> float:
    h = safe_hex(hex_color).lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def on_color(hex_color: str) -> str:
    return "#FFFFFF" if luminance(hex_color) < 0.6 else INK


def readable_brand(hex_color: str) -> str:
    """Brand color for text on white: darkened when the chosen color is too light to read."""
    h = safe_hex(hex_color)
    if luminance(h) < 0.55:
        return h
    base = h.lstrip("#")
    ink = INK.lstrip("#")
    mixed = "".join(
        format(round(int(base[i:i + 2], 16) * 0.35 + int(ink[i:i + 2], 16) * 0.65), "02X") for i in (0, 2, 4)
    )
    return "#" + mixed


def current_profile() -> dict:
    ss = st.session_state
    prof = {f: str(ss.get(f"p_{f}", "") or "").strip() for f in PROFILE_FIELDS}
    prof["color"] = safe_hex(ss.get("p_color"))
    prof["logo"] = ss.get("logo_b64", "") or ""
    return prof


def normalize_logo(data: bytes) -> bytes:
    """Downscale and convert to PNG so stored profiles stay small."""
    try:
        from PIL import Image as PILImage

        img = PILImage.open(io.BytesIO(data))
        img = img.convert("RGBA")
        img.thumbnail((600, 300))
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return data


# =========================================================================== #
# Pricing / payment schedule
# =========================================================================== #

def parse_budget(text: str) -> Decimal | None:
    """Return a single dollar amount, or None for ranges / ambiguous text."""
    if not text:
        return None
    t = text.lower().replace(",", "")
    nums = re.findall(r"(\d+(?:\.\d+)?)\s*(k|m)?\b", t)
    if len(nums) != 1:
        return None
    n, suffix = nums[0]
    val = Decimal(n)
    if suffix == "k":
        val *= 1000
    elif suffix == "m":
        val *= 1_000_000
    return val if val >= 500 else None


def money(val: Decimal) -> str:
    return "${:,.2f}".format(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def payment_table_md(schedule: str, total: Decimal | None) -> str:
    rows = SCHEDULES.get(schedule)
    if not rows:
        return ""
    amounts: list[str] = []
    if total is not None:
        running = Decimal("0")
        for idx, (pct, _, _) in enumerate(rows):
            if idx < len(rows) - 1:
                amt = (total * pct / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                running += amt
            else:
                amt = total - running
            amounts.append(money(amt))
    else:
        amounts = ["[$ TBD]"] * len(rows)
    lines = ["| Milestone | % | Amount | Due |", "|---|---|---|---|"]
    for (pct, name, due), amt in zip(rows, amounts):
        lines.append(f"| {name} | {pct}% | {amt} | {due} |")
    lines.append(f"| **Total** | **100%** | **{money(total) if total is not None else '[$ TOTAL]'}** | |")
    return "\n".join(lines)


def inject_payment_table(md: str, table_md: str) -> str:
    if not table_md:
        return md.replace(PAYMENT_MARKER, "")
    if PAYMENT_MARKER in md:
        return md.replace(PAYMENT_MARKER, "\n" + table_md + "\n")
    if re.search(r"\|\s*Milestone", md):
        return md
    m = re.search(r"^## 5\.", md, re.M)
    if m:
        return md[:m.start()] + table_md + "\n\n" + md[m.start():]
    return md + "\n\n" + table_md


# =========================================================================== #
# Claude call
# =========================================================================== #

def build_user_message(d: dict, table_md: str) -> str:
    if table_md:
        pay_instr = (
            f"In Section 4, after stating the total investment, output the exact line {PAYMENT_MARKER} on its own "
            "line where the milestone payment table belongs. Do NOT write your own milestone table; the system "
            "inserts a verified one there."
        )
    else:
        pay_instr = (
            "Recommend a payment schedule appropriate for this project and write a markdown milestone table "
            "(Milestone | % | Amount | Due) in Section 4. Use real amounts only if a single budget figure was provided."
        )
    p = d["profile"]
    return f"""Create a proposal from the following contractor input.

TODAY'S DATE: {date.today().strftime('%B %d, %Y')}
CLIENT NAME: {d['client']}
CLIENT CONTACT: {d['client_contact'] or 'NOT PROVIDED'}
PROJECT SITE ADDRESS: {d['address'] or 'NOT PROVIDED'}
PROJECT TYPE: {d['project_type']}
PRICING FORMAT: {d['pricing_format']}
BUDGET / PRICE (as given by contractor): {d['budget'] or 'NOT PROVIDED'}
PREFERRED PAYMENT SCHEDULE: {d['payment']}
CONTRACTOR COMPANY: {p['company'] or 'NOT PROVIDED'}
CONTRACTOR STATE / LICENSE #: {p['state'] or 'NOT PROVIDED'} / {p['license'] or 'NOT PROVIDED'}

PAYMENT TABLE INSTRUCTION: {pay_instr}

RAW PROJECT NOTES (may be unstructured voice-to-text; clean up, organize, and fill in standard trade scope that \
logically belongs with this work, while flagging assumptions in the Contractor Review Notes):
\"\"\"
{d['notes']}
\"\"\"
"""


def stream_proposal(client: Anthropic, model: str, user_msg: str):
    with client.messages.stream(
        model=model,
        max_tokens=7000,
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


# =========================================================================== #
# On-screen preview (Markdown -> HTML on a letterhead "paper")
# =========================================================================== #

def _inline_html(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"\[([^\]\n]{2,80})\]", r"<mark>[\1]</mark>", t)
    return t


def md_to_html(md: str) -> str:
    parts: list[str] = []
    lines = md.splitlines()
    i = 0
    list_tag: str | None = None

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            parts.append(f"</{list_tag}>")
            list_tag = None

    while i < len(lines):
        raw = lines[i].rstrip()
        s = raw.strip()
        if not s:
            close_list()
            i += 1
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s):
            close_list()
            parts.append("<hr>")
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", s)
        if m:
            close_list()
            lvl = min(len(m.group(1)), 3)
            parts.append(f"<h{lvl}>{_inline_html(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if s.startswith("|"):
            close_list()
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                n = max(len(r) for r in rows)
                head = "".join(f"<th>{_inline_html(c)}</th>" for c in rows[0] + [""] * (n - len(rows[0])))
                body = "".join(
                    "<tr>" + "".join(f"<td>{_inline_html(c)}</td>" for c in r + [""] * (n - len(r))) + "</tr>"
                    for r in rows[1:]
                )
                parts.append(f'<div class="tbl"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')
            continue
        m = re.match(r"^\s*[-*+]\s+(.*)", raw)
        if m:
            if list_tag != "ul":
                close_list()
                parts.append("<ul>")
                list_tag = "ul"
            parts.append(f"<li>{_inline_html(m.group(1))}</li>")
            i += 1
            continue
        m = re.match(r"^\s*(\d+)[.)]\s+(.*)", raw)
        if m:
            if list_tag != "ol":
                close_list()
                parts.append("<ol>")
                list_tag = "ol"
            parts.append(f"<li>{_inline_html(m.group(2))}</li>")
            i += 1
            continue
        close_list()
        parts.append(f"<p>{_inline_html(s)}</p>")
        i += 1
    close_list()
    # '$' would otherwise trigger LaTeX rendering inside st.markdown
    return "".join(parts).replace("$", "&#36;")


def paper_html(md: str, p: dict, streaming: bool = False, empty: bool = False) -> str:
    brand = safe_hex(p.get("color"))
    on_brand = on_color(brand)
    name = html.escape(p.get("company", "")) or '<span class="ghost">Your company name</span>'
    logo = (
        f'<img class="lh-logo" alt="" src="data:image/png;base64,{p["logo"]}">' if p.get("logo") else ""
    )
    meta_bits = []
    if p.get("license"):
        lic = f"License # {html.escape(p['license'])}"
        if p.get("state"):
            lic += f" ({html.escape(p['state'])})"
        meta_bits.append(lic)
    for key in ("phone", "email", "website", "address"):
        if p.get(key):
            meta_bits.append(html.escape(p[key]))
    meta = "<br>".join(meta_bits) or '<span class="ghost">Phone, email and license appear here</span>'

    if empty or not md:
        body = (
            '<p class="hint">Your proposal appears here as soon as you generate it. '
            "Your branding from the sidebar prints on this letterhead and on every PDF page.</p>"
            '<div class="skel" style="width:46%"></div><div class="skel" style="width:92%"></div>'
            '<div class="skel" style="width:84%"></div><div class="skel" style="width:90%"></div>'
            '<div class="skel" style="width:38%;margin-top:1.4rem"></div>'
            '<div class="skel" style="width:88%"></div><div class="skel" style="width:76%"></div>'
        )
    else:
        body = md_to_html(md) + ('<span class="caret">&#9612;</span>' if streaming else "")

    return (
        f'<div class="paper" style="--brand:{brand};--on-brand:{on_brand};--brand-text:{readable_brand(brand)}">'
        f'<div class="lh"><div class="lh-left">{logo}<div class="lh-name">{name}</div></div>'
        f'<div class="lh-meta">{meta}</div></div>{body}</div>'
    )


# =========================================================================== #
# PDF: multi-page contract
# =========================================================================== #

def _pdf_safe(text: str) -> str:
    text = (
        (text or "")
        .replace("\u2192", "->")
        .replace("\u2610", "[ ]")
        .replace("\u2713", "v")
        .replace("\u00a0", " ")
    )
    return text.encode("cp1252", "ignore").decode("cp1252")


def _esc(text: str) -> str:
    return html.escape(_pdf_safe(text), quote=False)


def _inline_pdf(text: str) -> str:
    t = _esc(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", t)
    t = re.sub(r"\[([^\]\n]{2,80})\]", r'<font backColor="#FFF1B8">[\1]</font>', t)
    return t


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _P(text: str, style: ParagraphStyle, **kw) -> Paragraph:
    """Paragraph that never raises on odd markup."""
    try:
        return Paragraph(text, style, **kw)
    except Exception:
        return Paragraph(_plain(text), style, **kw)


def _tint(color: colors.Color, amount: float = 0.92) -> colors.Color:
    return colors.Color(
        1 - (1 - color.red) * (1 - amount),
        1 - (1 - color.green) * (1 - amount),
        1 - (1 - color.blue) * (1 - amount),
    )


def _fit(text: str, font: str, size: float, max_w: float) -> str:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    if stringWidth(text, font, size) <= max_w:
        return text
    while text and stringWidth(text + "...", font, size) > max_w:
        text = text[:-1]
    return text + "..."


class _NumberedCanvas(rl_canvas.Canvas):
    """Canvas that draws running header/footer with 'Page X of Y' and initials boxes."""

    def __init__(self, *args, chrome=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages: list[dict] = []
        self._chrome = chrome or {}

    def showPage(self):
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            self._draw_chrome(total)
            super().showPage()
        super().save()

    def _draw_chrome(self, total: int) -> None:
        c = self._chrome
        brand = colors.HexColor(c.get("brand", DEFAULT_BRAND))
        brand_text = colors.HexColor(c.get("brand_text", c.get("brand", DEFAULT_BRAND)))
        w, h = letter
        lm, rm = 0.8 * inch, w - 0.8 * inch
        page = self._pageNumber

        self.saveState()
        # Running header (pages after the first)
        if page > 1:
            self.setFont("Helvetica-Bold", 8.5)
            self.setFillColor(brand_text)
            self.drawString(lm, h - 0.5 * inch, _fit(c.get("company", ""), "Helvetica-Bold", 8.5, 3.4 * inch))
            self.setFont("Helvetica", 8.5)
            self.setFillColor(colors.HexColor(STEEL))
            right = f"Construction Agreement  |  {c.get('client', '')}"
            self.drawRightString(rm, h - 0.5 * inch, _fit(right, "Helvetica", 8.5, 3.4 * inch))
            self.setStrokeColor(brand)
            self.setLineWidth(1.2)
            self.line(lm, h - 0.58 * inch, rm, h - 0.58 * inch)

        # Footer
        self.setStrokeColor(colors.HexColor(RULE))
        self.setLineWidth(0.6)
        self.line(lm, 0.78 * inch, rm, 0.78 * inch)
        self.setFont("Helvetica", 7.5)
        self.setFillColor(colors.HexColor(STEEL))
        contact = "  |  ".join(
            x for x in (
                c.get("company"),
                f"Lic. # {c['license']}" if c.get("license") else "",
                c.get("phone"),
                c.get("email"),
            ) if x
        )
        self.drawString(lm, 0.62 * inch, _fit(contact, "Helvetica", 7.5, 4.6 * inch))
        self.drawRightString(rm, 0.62 * inch, f"Page {page} of {total}")
        self.drawString(lm, 0.47 * inch, f"Contract No. {c.get('contract_no', '')}  |  Proposal valid for 30 days")
        self.drawRightString(rm, 0.47 * inch, "Initials:  Contractor ______   Client ______")
        self.restoreState()


def _build_styles(brand: colors.Color, on_brand: colors.Color, brand_text: colors.Color) -> dict:
    ink = colors.HexColor(INK)
    steel = colors.HexColor(STEEL)

    def ps(name, **kw):
        base = dict(fontName="Helvetica", fontSize=9.5, leading=13.5, textColor=ink)
        base.update(kw)
        return ParagraphStyle(name, **base)

    return {
        "lh_name": ps("lh_name", fontName="Helvetica-Bold", fontSize=18, leading=21, textColor=brand_text),
        "lh_sub": ps("lh_sub", fontSize=8.5, leading=11.5, textColor=steel),
        "lh_contact": ps("lh_contact", fontSize=8.5, leading=11.5, textColor=steel, alignment=TA_RIGHT),
        "doc_title": ps("doc_title", fontName="Helvetica-Bold", fontSize=15, leading=19),
        "doc_sub": ps("doc_sub", fontSize=10, leading=13, textColor=steel),
        "doc_meta": ps("doc_meta", fontSize=8.5, leading=12, textColor=steel, alignment=TA_RIGHT),
        "party_h": ps("party_h", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=on_brand),
        "label": ps("label", fontName="Helvetica-Bold", fontSize=8, leading=11, textColor=steel),
        "cell": ps("cell", fontSize=9, leading=12.5),
        "cell_r": ps("cell_r", fontSize=9, leading=12.5, alignment=TA_RIGHT),
        "cell_h": ps("cell_h", fontName="Helvetica-Bold", fontSize=8.8, leading=11.5, textColor=on_brand),
        "cell_hr": ps("cell_hr", fontName="Helvetica-Bold", fontSize=8.8, leading=11.5, textColor=on_brand, alignment=TA_RIGHT),
        "band": ps("band", fontName="Helvetica-Bold", fontSize=10.5, leading=13, textColor=on_brand),
        "h3": ps("h3", fontName="Helvetica-Bold", fontSize=10.5, leading=14, textColor=brand_text,
                 spaceBefore=8, spaceAfter=3, keepWithNext=1),
        "body": ps("body", spaceAfter=5),
        "sig_cap": ps("sig_cap", fontSize=8, leading=10.5, textColor=steel),
        "sig_txt": ps("sig_txt", fontSize=9.5, leading=13),
    }


def _letterhead(profile: dict, S: dict, brand: colors.Color, width: float) -> list:
    name = _P(_esc(profile.get("company") or "Contractor"), S["lh_name"])
    sub_bits = []
    if profile.get("license"):
        lic = f"License # {_esc(profile['license'])}"
        if profile.get("state"):
            lic += f" ({_esc(profile['state'])})"
        sub_bits.append(lic)
    name_cell = [name] + ([_P("<br/>".join(sub_bits), S["lh_sub"])] if sub_bits else [])

    contact_lines = [_esc(profile[k]) for k in ("phone", "email", "website", "address") if profile.get(k)]
    contact_cell = _P("<br/>".join(contact_lines), S["lh_contact"]) if contact_lines else ""

    logo_flow = None
    if profile.get("logo"):
        try:
            raw = base64.b64decode(profile["logo"])
            iw, ih = ImageReader(io.BytesIO(raw)).getSize()
            scale = min(1.5 * inch / iw, 0.8 * inch / ih)
            logo_flow = Image(io.BytesIO(raw), iw * scale, ih * scale)
        except Exception:
            logo_flow = None

    if logo_flow is not None:
        widths = [1.7 * inch, width * 0.47, width - 1.7 * inch - width * 0.47]
        data = [[logo_flow, name_cell, contact_cell]]
    else:
        widths = [width * 0.55, width * 0.45]
        data = [[name_cell, contact_cell]]
    t = Table(data, colWidths=widths)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [t, HRFlowable(width="100%", thickness=3, color=brand, spaceBefore=2, spaceAfter=10)]


def _title_block(meta: dict, S: dict, width: float) -> Table:
    issued = meta["date"]
    valid = issued + timedelta(days=30)
    left = [
        _P("RESIDENTIAL CONSTRUCTION PROPOSAL &amp; AGREEMENT", S["doc_title"]),
        _P(_esc(meta.get("project_type", "")), S["doc_sub"]),
    ]
    right = [
        _P(f"<b>Contract No.</b> {_esc(meta['contract_no'])}", S["doc_meta"]),
        _P(f"<b>Date issued:</b> {issued.strftime('%B %d, %Y')}", S["doc_meta"]),
        _P(f"<b>Valid through:</b> {valid.strftime('%B %d, %Y')}", S["doc_meta"]),
    ]
    t = Table([[left, right]], colWidths=[width * 0.62, width * 0.38])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _parties_block(profile: dict, meta: dict, S: dict, brand: colors.Color, width: float) -> Table:
    contractor = [f"<b>{_esc(profile.get('company') or 'Contractor')}</b>"]
    if profile.get("license"):
        lic = f"License # {_esc(profile['license'])}"
        if profile.get("state"):
            lic += f" ({_esc(profile['state'])})"
        contractor.append(lic)
    for k in ("phone", "email", "address"):
        if profile.get(k):
            contractor.append(_esc(profile[k]))
    client = [f"<b>{_esc(meta.get('client', ''))}</b>"]
    if meta.get("client_contact"):
        client.append(_esc(meta["client_contact"]))
    site = _esc(meta.get("address") or "[Project address to be confirmed]")

    data = [
        [_P("CONTRACTOR", S["party_h"]), _P("CLIENT", S["party_h"])],
        [_P("<br/>".join(contractor), S["cell"]), _P("<br/>".join(client), S["cell"])],
        [_P(f"<b>Project site:</b> {site}", S["cell"]), ""],
    ]
    t = Table(data, colWidths=[width / 2, width / 2])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), brand),
        ("SPAN", (0, 2), (1, 2)),
        ("BACKGROUND", (0, 2), (-1, 2), _tint(brand)),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(RULE)),
        ("LINEAFTER", (0, 0), (0, 1), 0.6, colors.HexColor(RULE)),
        ("LINEABOVE", (0, 2), (-1, 2), 0.6, colors.HexColor(RULE)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _glance_block(meta: dict, S: dict, brand: colors.Color, width: float) -> Table:
    valid = meta["date"] + timedelta(days=30)
    data = [
        [_P("PROJECT TYPE", S["label"]), _P(_esc(meta.get("project_type", "")), S["cell"]),
         _P("CONTRACT PRICE", S["label"]), _P(f"<b>{_esc(meta.get('price', ''))}</b>", S["cell"])],
        [_P("PAYMENT SCHEDULE", S["label"]), _P(_esc(meta.get("payment_label", "")), S["cell"]),
         _P("OFFER EXPIRES", S["label"]), _P(valid.strftime("%B %d, %Y"), S["cell"])],
    ]
    t = Table(data, colWidths=[width * 0.19, width * 0.31, width * 0.19, width * 0.31])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), _tint(brand)),
        ("BACKGROUND", (2, 0), (2, -1), _tint(brand)),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(RULE)),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.HexColor(RULE)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _section_band(text: str, S: dict, brand: colors.Color, width: float) -> list:
    band = Table([[_P(_inline_pdf(text.upper()), S["band"])]], colWidths=[width])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), brand),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    band.keepWithNext = True
    return [Spacer(1, 12), band, Spacer(1, 6)]


_NUMERIC_CELL = re.compile(r"^\**\s*[\$\u20ac]?[\d,\.]+\s*%?\**$|^\**\[\$[^\]]*\]\**$")


def _make_table(rows: list[list[str]], S: dict, brand: colors.Color, width: float) -> Table:
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]

    numeric = []
    for c in range(ncols):
        body = [r[c].strip() for r in rows[1:] if r[c].strip()]
        numeric.append(bool(body) and all(_NUMERIC_CELL.match(x) for x in body))

    maxlens = [max(len(_plain(re.sub(r"\*", "", r[c]))) for r in rows) for c in range(ncols)]
    weights = [min(max(l + 4, 9), 46) for l in maxlens]
    weights = [max(w, 14) if numeric[c] and maxlens[c] > 5 else w for c, w in enumerate(weights)]
    scale = width / sum(weights)
    widths = [w * scale for w in weights]

    data = []
    for r_idx, r in enumerate(rows):
        row = []
        for c, cell in enumerate(r):
            if r_idx == 0:
                st_ = S["cell_hr"] if numeric[c] else S["cell_h"]
            else:
                st_ = S["cell_r"] if numeric[c] else S["cell"]
            row.append(_P(_inline_pdf(cell), st_))
        data.append(row)

    t = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), brand),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F6F9")]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(RULE)),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor(RULE)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
    ]
    if len(rows) > 2 and re.sub(r"\*", "", rows[-1][0]).strip().lower() == "total":
        style += [
            ("BACKGROUND", (0, -1), (-1, -1), _tint(brand, 0.85)),
            ("LINEABOVE", (0, -1), (-1, -1), 1.2, brand),
        ]
    t.setStyle(TableStyle(style))
    return t


def _signature_block(profile: dict, meta: dict, S: dict, width: float, with_label: bool) -> KeepTogether:
    gap = width * 0.08
    col = (width - gap) / 2
    line = colors.HexColor(INK)
    client = _esc(meta.get("client", ""))
    company = _esc(profile.get("company") or "Contractor")
    data = [
        ["", "", ""],
        [_P("Client signature", S["sig_cap"]), "", _P("Contractor signature (authorized representative)", S["sig_cap"])],
        [_P(f"Printed name: {client}", S["sig_txt"]), "", _P(f"Company: {company}", S["sig_txt"])],
        [_P("Date: ____________________", S["sig_txt"]), "", _P("Date: ____________________", S["sig_txt"])],
    ]
    t = Table(data, colWidths=[col, gap, col], rowHeights=[34, None, None, 22])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (0, 0), 0.9, line),
        ("LINEBELOW", (2, 0), (2, 0), 0.9, line),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    items: list = [Spacer(1, 14)]
    if with_label:
        items.append(_P("<b>Authorization and signatures</b>", S["sig_txt"]))
        items.append(Spacer(1, 4))
    items.append(t)
    return KeepTogether(items)


def _md_flowables(md: str, S: dict, brand: colors.Color, width: float) -> list:
    story: list = []
    lines = md.splitlines()
    i = 0
    seen_h1 = False
    while i < len(lines):
        raw = lines[i].rstrip()
        s = raw.strip()
        if not s:
            i += 1
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s):
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor(RULE), spaceBefore=6, spaceAfter=6))
            i += 1
            continue
        # Content the template supplies itself
        if re.match(r"(?i)^\**prepared for", s):
            i += 1
            continue
        if re.match(r"(?i)^(client|contractor|owner)[^:]{0,24}signature", s) or re.fullmatch(r"[_\s]+", s):
            i += 1
            continue

        m = re.match(r"^(#{1,4})\s+(.*)", s)
        if m:
            level, txt = len(m.group(1)), m.group(2)
            if level == 1:
                if not seen_h1:
                    seen_h1 = True
                    i += 1
                    continue
                story.append(_P(_inline_pdf(txt), S["h3"]))
            elif level == 2:
                story += _section_band(txt, S, brand, width)
            else:
                story.append(_P(_inline_pdf(txt), S["h3"]))
            i += 1
            continue

        if s.startswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                tbl = _make_table(rows, S, brand, width)
                story.append(Spacer(1, 3))
                story.append(KeepTogether([tbl]) if len(rows) <= 9 else tbl)
                story.append(Spacer(1, 8))
            continue

        m = re.match(r"^(\s*)[-*+]\s+(.*)", raw)
        if m:
            depth = len(m.group(1).replace("\t", "  ")) // 2
            style = ParagraphStyle(
                f"bul{depth}", parent=S["body"], leftIndent=15 + depth * 14, bulletIndent=3 + depth * 14, spaceAfter=2.5
            )
            story.append(_P(_inline_pdf(m.group(2)), style, bulletText="\u2022" if depth == 0 else "-"))
            i += 1
            continue

        m = re.match(r"^\s*(\d+)[.)]\s+(.*)", raw)
        if m:
            style = ParagraphStyle("num", parent=S["body"], leftIndent=19, bulletIndent=0, spaceAfter=2.5)
            story.append(_P(_inline_pdf(m.group(2)), style, bulletText=f"{m.group(1)}."))
            i += 1
            continue

        story.append(_P(_inline_pdf(s), S["body"]))
        i += 1
    return story


def build_contract_pdf(md: str, profile: dict, meta: dict) -> bytes:
    """Render the proposal as a branded, multi-page construction contract."""
    brand_hex = safe_hex(profile.get("color"))
    brand = colors.HexColor(brand_hex)
    on_brand = colors.HexColor(on_color(brand_hex))
    brand_text = colors.HexColor(readable_brand(brand_hex))
    S = _build_styles(brand, on_brand, brand_text)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.8 * inch,
        rightMargin=0.8 * inch,
        topMargin=0.85 * inch,
        bottomMargin=1.0 * inch,
        title=f"{meta.get('project_type', 'Project')} Proposal - {meta.get('client', '')}",
        author=profile.get("company") or APP_NAME,
        subject="Residential construction proposal and agreement",
        creator=APP_NAME,
    )
    width = doc.width

    story: list = []
    story += _letterhead(profile, S, brand, width)
    story.append(_title_block(meta, S, width))
    story.append(Spacer(1, 12))
    story.append(_parties_block(profile, meta, S, brand, width))
    story.append(Spacer(1, 8))
    story.append(_glance_block(meta, S, brand, width))
    story += _md_flowables(_pdf_safe(md), S, brand, width)
    story.append(_signature_block(profile, meta, S, width, with_label="acceptance" not in md.lower()))

    chrome = {
        "brand": brand_hex,
        "brand_text": readable_brand(brand_hex),
        "company": _pdf_safe(profile.get("company") or "Contractor"),
        "license": _pdf_safe(profile.get("license", "")),
        "phone": _pdf_safe(profile.get("phone", "")),
        "email": _pdf_safe(profile.get("email", "")),
        "client": _pdf_safe(meta.get("client", "")),
        "contract_no": meta["contract_no"],
    }
    doc.build(story, canvasmaker=lambda *a, **k: _NumberedCanvas(*a, chrome=chrome, **k))
    return buf.getvalue()


# =========================================================================== #
# Styling
# =========================================================================== #

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700;800&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

:root{
  --ink:#0F1B2D; --blueprint:#1E4E8C; --blueprint-deep:#163B6B; --plaster:#F3F4F6;
  --steel:#5A6778; --rule:#D8DDE5; --tape:#F5B700; --tape-deep:#D9A400;
}
html, body, .stApp, [data-testid="stAppViewContainer"]{background:var(--plaster) !important; color:var(--ink);}
.stApp p, .stApp li, .stApp label, .stApp input, .stApp textarea, .stApp button, .stApp [data-baseweb="select"]{
  font-family:'IBM Plex Sans', -apple-system, 'Segoe UI', Roboto, sans-serif;
}
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5{
  font-family:'Archivo','IBM Plex Sans',sans-serif !important; color:var(--ink); letter-spacing:-0.01em;
}
#MainMenu, footer, [data-testid="stDecoration"]{visibility:hidden; height:0;}
.block-container{max-width:1280px; padding-top:1.1rem; padding-bottom:4rem;}

/* ---- sidebar ---- */
[data-testid="stSidebar"]{background:#fff; border-right:1px solid var(--rule);}
[data-testid="stSidebar"] .block-container, [data-testid="stSidebarUserContent"]{padding-top:1rem;}
.side-brand{display:flex; align-items:center; gap:.6rem; font-family:'Archivo',sans-serif; font-weight:800; font-size:1.15rem; color:var(--ink); margin-bottom:.4rem;}
.side-h{font-family:'Archivo',sans-serif; font-weight:700; font-size:.95rem; color:var(--ink); margin:1.1rem 0 .15rem;}
.side-sub{color:var(--steel); font-size:.8rem; margin-bottom:.5rem; line-height:1.35;}
.pill{display:inline-block; border-radius:999px; padding:.18rem .65rem; font-size:.78rem; font-weight:600;}
.pill.pro{background:var(--tape); color:var(--ink);}
.pill.free{background:#E7EEF8; color:var(--blueprint-deep);}

/* ---- hero ---- */
.ts-hero{
  background-color:#173F73;
  background-image:linear-gradient(rgba(255,255,255,.06) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.06) 1px, transparent 1px);
  background-size:22px 22px; border-radius:12px; padding:1.5rem 1.75rem;
  display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap; margin-bottom:1.25rem;
}
.ts-brand{display:flex; align-items:center; gap:.85rem;}
.ts-name{font-family:'Archivo',sans-serif; font-weight:800; font-size:1.55rem; color:#fff; line-height:1.1;}
.ts-line{color:#C9D8EE; font-size:.95rem; margin-top:.2rem;}
.chip{background:rgba(255,255,255,.12); border:1px solid rgba(255,255,255,.3); color:#fff; border-radius:999px; padding:.4rem .95rem; font-size:.86rem; font-weight:500;}
.chip.pro{background:var(--tape); border-color:var(--tape-deep); color:var(--ink); font-weight:600;}

/* ---- form card ---- */
[data-testid="stForm"]{background:#fff; border:1px solid var(--rule); border-radius:10px; padding:1.3rem 1.4rem 1.2rem;}
.card-title{font-family:'Archivo',sans-serif; font-weight:700; font-size:1.15rem; color:var(--ink); margin-bottom:.1rem;}
.card-sub{color:var(--steel); font-size:.86rem; margin-bottom:.7rem;}
[data-testid="stWidgetLabel"] p{font-size:.85rem; font-weight:600; color:var(--ink);}
.stTextInput input, .stTextArea textarea, [data-baseweb="select"] > div{background:#fff !important; color:var(--ink) !important; border-radius:8px !important;}
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div{border-color:var(--rule) !important; border-radius:8px !important;}
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within{border-color:var(--blueprint) !important; box-shadow:0 0 0 2px rgba(30,78,140,.18) !important;}

/* ---- buttons ---- */
[data-testid="stFormSubmitButton"] button, button[kind="primary"], [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"]{
  background:var(--tape) !important; color:var(--ink) !important; border:1px solid var(--tape-deep) !important;
  font-weight:700 !important; border-radius:8px !important; padding:.6rem 1rem !important; width:100%;
}
[data-testid="stFormSubmitButton"] button:hover, button[kind="primary"]:hover, [data-testid="stBaseButton-primary"]:hover{background:#FFC928 !important; border-color:var(--tape-deep) !important;}
[data-testid="stFormSubmitButton"] button p, button[kind="primary"] p{color:var(--ink) !important; font-weight:700;}
[data-testid="stBaseButton-secondary"]{background:#fff; color:var(--ink); border:1px solid var(--rule); border-radius:8px; font-weight:600;}
[data-testid="stBaseButton-secondary"]:hover{border-color:var(--blueprint); color:var(--blueprint);}
[data-testid="stDownloadButton"] button{background:var(--blueprint) !important; border:1px solid var(--blueprint-deep) !important; border-radius:8px !important; width:100%; padding:.55rem 1rem !important;}
[data-testid="stDownloadButton"] button:hover{background:var(--blueprint-deep) !important;}
[data-testid="stDownloadButton"] button p{color:#fff !important; font-weight:600;}

/* ---- tabs ---- */
button[data-baseweb="tab"] p{font-weight:600;}
[data-baseweb="tab-highlight"]{background-color:var(--blueprint) !important;}

/* ---- the proposal sheet ---- */
.paper{background:#fff; border:1px solid var(--rule); border-radius:4px; padding:2rem 2.3rem 2.2rem; box-shadow:0 1px 2px rgba(15,27,45,.06), 0 10px 28px rgba(15,27,45,.07); color:#1B2433; font-size:.94rem; line-height:1.55;}
.paper .lh{display:flex; justify-content:space-between; align-items:flex-start; gap:1rem; flex-wrap:wrap; border-bottom:3px solid var(--brand); padding-bottom:.9rem; margin-bottom:1.1rem;}
.paper .lh-left{display:flex; align-items:center; gap:.9rem;}
.paper .lh-logo{max-height:56px; max-width:130px; object-fit:contain;}
.paper .lh-name{font-family:'Archivo',sans-serif; font-weight:800; font-size:1.4rem; color:var(--brand-text); line-height:1.15;}
.paper .lh-meta{color:var(--steel); font-size:.8rem; text-align:right; line-height:1.5;}
.paper .ghost{color:#A4AEBC; font-weight:500;}
.paper h1{font-family:'Archivo',sans-serif; font-size:1.35rem; margin:.2rem 0 .5rem; color:var(--ink);}
.paper h2{font-family:'Archivo',sans-serif; background:var(--brand); color:var(--on-brand); font-size:.95rem; padding:.45rem .75rem; margin:1.5rem 0 .6rem; border-radius:2px;}
.paper h3{font-family:'Archivo',sans-serif; color:var(--brand-text); font-size:1rem; margin:1.1rem 0 .3rem;}
.paper p{margin:.35rem 0;}
.paper ul, .paper ol{margin:.3rem 0 .6rem 1.2rem; padding:0;}
.paper li{margin:.15rem 0;}
.paper hr{border:none; border-top:1px solid var(--rule); margin:1rem 0;}
.paper mark{background:#FFF1B8; color:inherit; padding:0 .15rem; border-radius:2px;}
.paper .tbl{overflow-x:auto; margin:.6rem 0;}
.paper table{border-collapse:collapse; width:100%; font-size:.86rem;}
.paper th{background:var(--brand); color:var(--on-brand); text-align:left; padding:.45rem .6rem; font-weight:600;}
.paper td{border-bottom:1px solid var(--rule); padding:.45rem .6rem; vertical-align:top;}
.paper tbody tr:nth-child(even) td{background:#F6F8FB;}
.paper .hint{color:var(--steel); margin-bottom:1.2rem;}
.paper .skel{height:.62rem; border-radius:3px; background:#E9EDF2; margin:.6rem 0;}
.paper .caret{color:var(--brand-text); animation:blink 1s steps(2,start) infinite;}
@keyframes blink{to{visibility:hidden;}}
@media (prefers-reduced-motion: reduce){.paper .caret{animation:none;}}
@media (max-width:640px){.paper{padding:1.2rem 1rem;} .ts-hero{padding:1.1rem;} .paper .lh-meta{text-align:left;}}

/* ---- paywall + plans ---- */
.paywall{background:#fff; border:1px solid var(--rule); border-left:5px solid var(--tape); border-radius:8px; padding:1rem 1.2rem; margin-bottom:1rem;}
.paywall b{font-family:'Archivo',sans-serif; font-size:1.05rem;}
.paywall p{margin:.3rem 0 0; color:var(--steel); font-size:.9rem;}
.plan{border:1px solid var(--rule); border-radius:10px; padding:1rem 1.1rem; background:#fff; height:100%;}
.plan.pro{border-color:var(--blueprint); box-shadow:inset 0 0 0 1px var(--blueprint);}
.plan .p-name{font-family:'Archivo',sans-serif; font-weight:700; font-size:1.05rem;}
.plan .p-price{font-family:'Archivo',sans-serif; font-weight:800; font-size:1.7rem; margin:.2rem 0;}
.plan .p-price small{font-size:.85rem; font-weight:500; color:var(--steel);}
.plan ul{margin:.5rem 0 0 1.1rem; padding:0; color:#2B3645; font-size:.9rem;}
.plan .p-tag{font-size:.78rem; font-weight:600; color:var(--blueprint-deep);}
</style>
"""

LOGO_MARK = (
    '<svg width="40" height="40" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<rect width="40" height="40" rx="9" fill="#FFFFFF" fill-opacity="0.12" stroke="#FFFFFF" stroke-opacity="0.35"/>'
    '<path d="M11 10 H15 V25 H30 V29 H11 Z" fill="#F5B700"/>'
    '<path d="M19 10 H21 V12 H19 Z M23 10 H25 V12 H23 Z M27 10 H29 V12 H27 Z" fill="#FFFFFF" fill-opacity="0.7"/>'
    "</svg>"
)
LOGO_MARK_DARK = LOGO_MARK.replace('fill="#FFFFFF" fill-opacity="0.12" stroke="#FFFFFF" stroke-opacity="0.35"',
                                   'fill="#173F73"').replace('width="40" height="40"', 'width="30" height="30"', 1)


# =========================================================================== #
# UI pieces
# =========================================================================== #

def copy_button(text: str) -> None:
    payload = json.dumps(text).replace("</", "<\\/")
    components.html(
        f"""
        <button id="copy-btn" style="
            width:100%; padding:.58rem 1rem; border-radius:8px; border:1px solid {RULE};
            background:#ffffff; color:{INK}; font-size:.95rem; font-weight:600; cursor:pointer;
            font-family:'IBM Plex Sans',-apple-system,'Segoe UI',Roboto,sans-serif;">
            Copy to clipboard
        </button>
        <script>
            const text = {payload};
            const btn = document.getElementById('copy-btn');
            function done(ok) {{
                btn.textContent = ok ? 'Copied' : 'Copy failed. Select the text in the Edit tab.';
                setTimeout(() => btn.textContent = 'Copy to clipboard', 2200);
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
        height=50,
    )


def plan_html(name: str, price: str, per: str, bullets: list[str], current: bool, pro: bool = False) -> str:
    tag = '<div class="p-tag">Your current plan</div>' if current else ""
    items = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
    return (
        f'<div class="plan{" pro" if pro else ""}">{tag}<div class="p-name">{html.escape(name)}</div>'
        f'<div class="p-price">{price}<small> {html.escape(per)}</small></div><ul>{items}</ul></div>'
    )


@st.dialog("Choose your plan", width="large")
def plans_dialog(aid: str) -> None:
    acc = get_account(aid)
    is_pro = acc.get("plan") == "pro"
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(
            plan_html(
                "Free trial", "&#36;0", f"for {FREE_TRIAL_LIMIT} proposals",
                ["Full AI proposals", "Branded PDF contracts", "Copy and edit before sending"],
                current=not is_pro,
            ),
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            plan_html(
                "Pro", "&#36;49", "per month",
                ["Unlimited proposals", "Everything in the trial", "Priority access to new features"],
                current=is_pro, pro=True,
            ),
            unsafe_allow_html=True,
        )
    st.write("")

    if is_pro:
        st.success("Pro is active on this account.")
        if demo_billing_enabled() and st.button("Cancel Pro (demo)", use_container_width=True):
            set_plan(aid, "free")
            st.session_state["_toast"] = "Switched back to the free trial."
            st.rerun()
        return

    link = get_secret("STRIPE_PAYMENT_LINK", "https://buy.stripe.com/5kQeVe2e0bWk9yQ8Q2es000")
    if link:
        st.link_button("Subscribe with card", link, type="primary", use_container_width=True)
        st.caption("After checkout you receive an activation code. Enter it below.")
    code = st.text_input("Activation code", key="act_code", placeholder="Paste your code")
    if st.button("Activate code", use_container_width=True):
        if code.strip().upper() in valid_codes():
            set_plan(aid, "pro")
            st.session_state["_toast"] = "Pro activated. Proposals are now unlimited."
            st.rerun()
        else:
            st.error("That code isn't valid. Check it and try again.")
    if demo_billing_enabled():
        st.divider()
        st.caption("Demo mode: this upgrade is simulated and nothing is charged.")
        if st.button("Activate Pro (demo)", type="primary", use_container_width=True):
            set_plan(aid, "pro")
            st.session_state["_toast"] = "Pro activated (demo). Proposals are now unlimited."
            st.rerun()


def init_state() -> None:
    ss = st.session_state
    ss.setdefault("proposal_text", "")
    ss.setdefault("review_notes", "")
    ss.setdefault("meta", {})
    ss.setdefault("p_color", DEFAULT_BRAND)
    ss.setdefault("logo_b64", "")
    ss.setdefault("_logo_sig", "")


def render_sidebar() -> tuple[str | None, dict]:
    ss = st.session_state
    with st.sidebar:
        st.markdown(f'<div class="side-brand">{LOGO_MARK_DARK}<span>{APP_NAME}</span></div>', unsafe_allow_html=True)

        st.markdown('<div class="side-h">Account</div>', unsafe_allow_html=True)
        email = st.text_input(
            "Work email", key="p_email", placeholder="you@yourcompany.com",
            help="Your account and trial are tied to this email. It also prints on your PDFs.",
        )
        aid = account_id(email)

        # Load saved branding the first time an account email is entered
        if aid and ss.get("_loaded_for") != aid:
            ss["_loaded_for"] = aid
            saved = get_account(aid).get("profile") or {}
            if saved:
                for f in PROFILE_FIELDS:
                    if f != "email":
                        ss[f"p_{f}"] = saved.get(f, DEFAULT_BRAND if f == "color" else "")
                ss["logo_b64"] = saved.get("logo", "")

        acc = get_account(aid) if aid else None
        if not aid:
            st.markdown(
                '<div class="side-sub">Enter your email to start your free trial of '
                f"{FREE_TRIAL_LIMIT} proposals.</div>",
                unsafe_allow_html=True,
            )
        elif acc.get("plan") == "pro":
            st.markdown('<span class="pill pro">Pro plan</span>', unsafe_allow_html=True)
            st.markdown('<div class="side-sub">Unlimited proposals.</div>', unsafe_allow_html=True)
            if st.button("Manage plan", key="manage_plan", use_container_width=True):
                plans_dialog(aid)
        else:
            left = trial_remaining(acc)
            st.markdown('<span class="pill free">Free trial</span>', unsafe_allow_html=True)
            st.progress(min(1.0, acc.get("used", 0) / FREE_TRIAL_LIMIT), text=f"{left} of {FREE_TRIAL_LIMIT} proposals left")
            if st.button("Upgrade to Pro", key="upgrade_btn", use_container_width=True):
                plans_dialog(aid)

        st.markdown('<div class="side-h">Company branding</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="side-sub">Stamped on the letterhead, header and footer of every PDF.</div>',
            unsafe_allow_html=True,
        )
        st.text_input("Company name", key="p_company", placeholder="Northwind Builders LLC")
        st.text_input("Phone", key="p_phone", placeholder="(555) 123-4567")
        c1, c2 = st.columns([3, 2])
        with c1:
            st.text_input("License number", key="p_license", placeholder="CSLB 1234567")
        with c2:
            st.text_input("State", key="p_state", placeholder="CA")
        st.text_input("Business address (optional)", key="p_address", placeholder="123 Main St, San Jose, CA")
        st.text_input("Website (optional)", key="p_website", placeholder="northwindbuilders.com")
        st.color_picker("Brand color", key="p_color", help="Used for headings, bands and tables in your PDF.")

        up = st.file_uploader("Logo (PNG or JPG)", type=["png", "jpg", "jpeg"], key="logo_up")
        if up is not None:
            data = up.getvalue()
            sig = hashlib.md5(data).hexdigest()
            if sig != ss.get("_logo_sig"):
                ss["logo_b64"] = base64.b64encode(normalize_logo(data)).decode()
                ss["_logo_sig"] = sig
        if ss.get("logo_b64"):
            try:
                st.image(base64.b64decode(ss["logo_b64"]), width=120)
            except Exception:
                pass
            if st.button("Remove logo", key="rm_logo"):
                ss["logo_b64"] = ""
                st.rerun()

        if st.button("Save branding", key="save_brand", use_container_width=True):
            if not aid:
                st.warning("Enter a valid work email first so we know where to save this.")
            else:
                prof = current_profile()
                update_account(aid, lambda a: a.__setitem__("profile", prof))
                st.toast("Branding saved. It loads automatically next time you sign in with this email.")

        if not get_secret("ANTHROPIC_API_KEY"):
            with st.expander("Developer: API key"):
                st.text_input("Anthropic API key", type="password", key="manual_key", placeholder="sk-ant-...")

    return aid, current_profile()


def render_hero(aid: str | None, acc: dict | None) -> None:
    if not aid:
        chip = '<span class="chip">Enter your email in the sidebar to start your free trial</span>'
    elif acc and acc.get("plan") == "pro":
        chip = '<span class="chip pro">Pro plan: unlimited proposals</span>'
    else:
        chip = f'<span class="chip">Free trial: {trial_remaining(acc)} of {FREE_TRIAL_LIMIT} proposals left</span>'
    st.markdown(
        f'<div class="ts-hero"><div class="ts-brand">{LOGO_MARK}<div><div class="ts-name">{APP_NAME}</div>'
        f'<div class="ts-line">Walkthrough notes in, signable contract out.</div></div></div>{chip}</div>',
        unsafe_allow_html=True,
    )


def render_output(profile: dict) -> None:
    ss = st.session_state
    meta = ss.get("meta", {})

    tab_preview, tab_edit = st.tabs(["Preview", "Edit text"])
    with tab_edit:
        st.text_area(
            "Edit before you copy or download", key="proposal_text", height=520, label_visibility="collapsed"
        )
    text = ss["proposal_text"]
    with tab_preview:
        st.markdown(paper_html(text, profile), unsafe_allow_html=True)

    if not profile.get("company"):
        st.info("Add your company name, phone, email and license in the sidebar to brand the PDF letterhead.")

    safe_client = re.sub(r"[^A-Za-z0-9]+", "_", meta.get("client", "Client")).strip("_") or "Client"
    safe_type = re.sub(r"[^A-Za-z0-9]+", "_", meta.get("project_type", "Project")).strip("_") or "Project"
    filename = f"{safe_client}_{safe_type}_Proposal.pdf"

    st.write("")
    b1, b2 = st.columns(2)
    with b1:
        copy_button(text)
    with b2:
        try:
            pdf_meta = dict(meta)
            pdf_meta["date"] = date.fromisoformat(meta.get("date_iso", date.today().isoformat()))
            pdf_bytes = build_contract_pdf(text, profile, pdf_meta)
            st.download_button(
                "Download branded PDF", data=pdf_bytes, file_name=filename, mime="application/pdf",
                use_container_width=True,
            )
        except Exception as e:  # noqa: BLE001
            st.error(f"The PDF couldn't be built: {e}")

    if ss.get("review_notes"):
        with st.expander("Contractor review notes (private, not in the PDF)", expanded=True):
            st.markdown(ss["review_notes"].replace("$", "\\$"))

    st.caption(
        "AI-generated draft. Review scope, pricing and legal language, and have a licensed attorney in your "
        "state approve your contract terms before you send it."
    )


# =========================================================================== #
# App
# =========================================================================== #

def main() -> None:
    st.set_page_config(page_title=f"{APP_NAME} | Proposals and contracts", page_icon="🛠️",
                       layout="wide", initial_sidebar_state="expanded")
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()

    aid, profile = render_sidebar()
    acc = get_account(aid) if aid else None
    render_hero(aid, acc)

    if msg := st.session_state.pop("_toast", None):
        st.toast(msg)

    left, right = st.columns([5, 7], gap="large")

    # ---------------- left: input form ---------------- #
    with left:
        with st.form("proposal_form"):
            st.markdown(
                '<div class="card-title">New proposal</div>'
                '<div class="card-sub">Dictate or paste your notes. Messy is fine.</div>',
                unsafe_allow_html=True,
            )
            client_name = st.text_input("Client name", placeholder="Mark and Linda Johnson")
            c1, c2 = st.columns(2)
            with c1:
                project_type = st.selectbox("Project type", PROJECT_TYPES)
            with c2:
                pricing_format = st.selectbox("Pricing format", PRICING_FORMATS)
            notes = st.text_area(
                "Project notes",
                height=210,
                placeholder=("Sizes, materials, what's being demoed, fixtures and appliances, client wishes, "
                             "access issues, anything odd you saw on the walkthrough..."),
                help="On a phone, tap the microphone on your keyboard to dictate.",
            )
            c3, c4 = st.columns(2)
            with c3:
                budget = st.text_input("Price or budget", placeholder="$48,500")
            with c4:
                payment = st.selectbox("Payment milestones", PAYMENT_SCHEDULES)
            with st.expander("Client and site details (optional)"):
                client_contact = st.text_input("Client phone or email")
                site_address = st.text_input("Project site address")
            submitted = st.form_submit_button("Generate proposal", type="primary")

    # ---------------- right: output ---------------- #
    with right:
        out = st.container()

    if submitted:
        errors = []
        if not aid:
            errors.append("Enter a valid work email in the sidebar to start your trial.")
        if not client_name.strip():
            errors.append("Add the client's name.")
        if not notes.strip():
            errors.append("Add some project notes.")
        with out:
            if errors:
                for e in errors:
                    st.error(e)
            elif not can_generate(acc):
                st.warning("Your free trial is used up. Upgrade to Pro to keep generating proposals.")
            elif not get_api_key():
                st.error("No Anthropic API key found. Set ANTHROPIC_API_KEY or paste a key under "
                         "Developer in the sidebar.")
            else:
                schedule = payment
                total = parse_budget(budget)
                table_md = payment_table_md(schedule, total) if schedule in SCHEDULES else ""
                data = {
                    "client": client_name.strip(),
                    "client_contact": client_contact.strip(),
                    "address": site_address.strip(),
                    "project_type": project_type,
                    "pricing_format": pricing_format,
                    "budget": budget.strip(),
                    "payment": payment,
                    "notes": notes.strip(),
                    "profile": profile,
                }
                client = Anthropic(api_key=get_api_key())
                ph = st.empty()
                full = ""
                try:
                    ph.markdown(paper_html("", profile, empty=True), unsafe_allow_html=True)
                    for chunk in stream_proposal(client, get_model(), build_user_message(data, table_md)):
                        full += chunk
                        shown = full.split(NOTES_MARKER)[0].replace(PAYMENT_MARKER, "\n" + table_md + "\n")
                        ph.markdown(paper_html(shown, profile, streaming=True), unsafe_allow_html=True)
                    ph.empty()
                    proposal, review_notes = split_notes(full)
                    proposal = inject_payment_table(proposal, table_md)

                    if total is not None:
                        price_display = money(total)
                    else:
                        price_display = budget.strip() or "[Price to be confirmed]"
                    meta = {
                        "client": data["client"],
                        "client_contact": data["client_contact"],
                        "address": data["address"],
                        "project_type": project_type,
                        "price": price_display,
                        "payment_label": payment if payment in SCHEDULES else "Per milestone schedule below",
                        "contract_no": f"TS-{date.today():%Y%m%d}-{uuid.uuid4().hex[:4].upper()}",
                        "date_iso": date.today().isoformat(),
                    }
                    st.session_state["proposal_text"] = proposal
                    st.session_state["review_notes"] = review_notes
                    st.session_state["meta"] = meta
                    record_generation(aid, meta)
                    st.session_state["_toast"] = "Proposal ready."
                    st.rerun()
                except AuthenticationError:
                    ph.empty()
                    st.error("The Anthropic API key was rejected. Check the key and try again.")
                except RateLimitError:
                    ph.empty()
                    st.error("Rate limit reached. Wait a moment and try again. This did not use a trial credit.")
                except APIError as e:
                    ph.empty()
                    st.error(f"Anthropic API error: {e}. This did not use a trial credit.")
                except Exception as e:  # noqa: BLE001
                    ph.empty()
                    st.error(f"Something went wrong: {e}")

    # Persistent right-column content (runs on every rerun, including after generation)
    with out:
        if acc is not None and not can_generate(acc):
            st.markdown(
                '<div class="paywall"><b>Your free trial is complete</b>'
                f"<p>You've used all {FREE_TRIAL_LIMIT} free proposals. Pro is &#36;49 per month for unlimited "
                "proposals.</p></div>",
                unsafe_allow_html=True,
            )
            if st.button("See plans", key="paywall_btn", type="primary"):
                plans_dialog(aid)
        if st.session_state.get("proposal_text"):
            render_output(profile)
        elif not submitted:
            st.markdown(paper_html("", profile, empty=True), unsafe_allow_html=True)


if __name__ == "__main__":
    main()
