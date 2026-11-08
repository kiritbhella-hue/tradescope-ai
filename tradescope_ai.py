"""
TradeScope AI — proposal and contract generator for residential contractors.
(Fun edition: login required, 3 free proposals per verified account, then Stripe subscription.)

SETUP
=====
    pip install -r requirements.txt
    streamlit run tradescope_ai.py

Required secrets (.streamlit/secrets.toml locally, or your host's secrets panel):

    ANTHROPIC_API_KEY = "sk-ant-..."

HOW THE "NO REFRESH LOOPHOLE" WORKS
-----------------------------------
1. Login is required before anything else renders (Google sign-in via Streamlit's built-in OIDC).
2. Free attempts are stored server-side in a SQLite database keyed by the verified account email,
   not in the browser. Refreshing, clearing cookies, or opening a new tab changes nothing.
3. An attempt is reserved atomically *before* Claude is called (so opening many tabs and submitting
   at once can't squeeze out extra runs). It is refunded automatically if the API fails before any
   text is produced.
4. After 3 attempts the form locks and the Stripe subscription link is offered. Pro status is
   verified against the Stripe API (needs STRIPE_SECRET_KEY), not trusted from the browser.

Google login (recommended for launch):

    [auth]
    redirect_uri = "https://YOUR-APP-URL/oauth2callback"        # local: http://localhost:8501/oauth2callback
    cookie_secret = "any-long-random-string"
    client_id = "....apps.googleusercontent.com"
    client_secret = "..."
    server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

  Create the client at console.cloud.google.com > APIs & Services > Credentials > OAuth client ID
  (type: Web application) and add the same redirect URI there.

Stripe (subscriptions):

    STRIPE_PAYMENT_LINK = "https://buy.stripe.com/..."   # optional, defaults to the link in this file
    STRIPE_SECRET_KEY   = "rk_live_..."                  # restricted key: read access to Customers + Subscriptions

  In the Stripe dashboard, edit the Payment Link > After payment > "Don't show confirmation page" and
  redirect to  https://YOUR-APP-URL/?paid=1  so customers are unlocked the moment they return.

Optional secrets:
    PRO_EMAILS            comma-separated emails that always get unlimited access (you, friends, comps)
    ADMIN_EMAILS          comma-separated emails that see the "Owner checks" panel
    ALLOW_PASSWORD_LOGIN  "true"/"false". Email+password sign-up is a fallback for setups without Google
                          login. It is ON only when Google login isn't configured, unless you set this.
                          It is weaker: anyone can make unlimited fake emails. Use Google login in production.
    PRO_PRICE_LABEL       e.g. "$29/mo", shown next to the upgrade button
    SUPPORT_EMAIL         shown if automatic unlock isn't configured
    TRADESCOPE_MODEL      Claude model override
    TRADESCOPE_DB_PATH    SQLite file location (default .tradescope_data/tradescope.db)

IMPORTANT: put the database on persistent storage. Hosts with ephemeral disks (including Streamlit
Community Cloud) can wipe the file on restart, which would reset everyone's free attempts.
Use a host with a persistent volume (Render disk, Railway/Fly volume, a VPS) or swap the storage
layer for a hosted database.
"""

import base64
import hashlib
import hmac
import html
import io
import json
import os
import random
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote

import requests
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

FREE_ATTEMPTS = 3
DEFAULT_STRIPE_LINK = "https://buy.stripe.com/5kQeVe2e0bWk9yQ8Q2es000"
DB_PATH = os.environ.get("TRADESCOPE_DB_PATH", ".tradescope_data/tradescope.db")

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
# Secrets and API access
# =========================================================================== #

def get_secret(name: str, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


def get_api_key() -> str | None:
    return get_secret("ANTHROPIC_API_KEY") or None


def get_model() -> str:
    return str(get_secret("TRADESCOPE_MODEL", DEFAULT_MODEL))


def _csv_set(name: str) -> set[str]:
    raw = get_secret(name, "")
    items = raw if (hasattr(raw, "__iter__") and not isinstance(raw, str)) else str(raw).split(",")
    return {str(x).strip().lower() for x in items if str(x).strip()}


def pro_emails() -> set[str]:
    return _csv_set("PRO_EMAILS")


def admin_emails() -> set[str]:
    return _csv_set("ADMIN_EMAILS")


# =========================================================================== #
# Storage: accounts, attempts, users (SQLite, server-side)
# =========================================================================== #

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _connect() -> sqlite3.Connection:
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@st.cache_resource
def init_db() -> bool:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                email TEXT PRIMARY KEY,
                plan TEXT NOT NULL DEFAULT 'free',
                used INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL,
                last_login TEXT,
                profile TEXT NOT NULL DEFAULT '{}',
                stripe_checked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                salt BLOB NOT NULL,
                pw_hash BLOB NOT NULL,
                fails INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                ts TEXT NOT NULL,
                client TEXT,
                project TEXT
            );
            """
        )
    return True


def get_account(email: str) -> dict:
    with db() as c:
        c.execute("INSERT OR IGNORE INTO accounts(email, created) VALUES(?, ?)", (email, _iso(_utcnow())))
        row = c.execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
    acc = dict(row)
    try:
        acc["profile"] = json.loads(acc.get("profile") or "{}")
    except ValueError:
        acc["profile"] = {}
    return acc


def is_pro(email: str, acc: dict | None) -> bool:
    return email in pro_emails() or bool(acc and acc.get("plan") == "pro")


def attempts_left(acc: dict) -> int:
    return max(0, FREE_ATTEMPTS - int(acc.get("used", 0)))


def touch_login(email: str) -> None:
    with db() as c:
        c.execute("UPDATE accounts SET last_login = ? WHERE email = ?", (_iso(_utcnow()), email))


def save_profile(email: str, profile: dict) -> None:
    with db() as c:
        c.execute("UPDATE accounts SET profile = ? WHERE email = ?", (json.dumps(profile), email))


def reserve_attempt(email: str) -> str | None:
    """
    Atomically claim one generation. Returns "pro", "trial", or None (no attempts left).
    The check and the increment happen inside one write transaction, so simultaneous tabs
    cannot both slip through on the last free attempt.
    """
    unlimited_by_email = email in pro_emails()
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT plan, used FROM accounts WHERE email = ?", (email,)).fetchone()
            if row is None:
                c.execute("ROLLBACK")
                return None
            if unlimited_by_email or row["plan"] == "pro":
                c.execute("COMMIT")
                return "pro"
            if row["used"] >= FREE_ATTEMPTS:
                c.execute("ROLLBACK")
                return None
            c.execute("UPDATE accounts SET used = used + 1 WHERE email = ?", (email,))
            c.execute("COMMIT")
            return "trial"
        except Exception:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            raise


def refund_attempt(email: str) -> None:
    with db() as c:
        c.execute("UPDATE accounts SET used = MAX(used - 1, 0) WHERE email = ?", (email,))


def log_generation(email: str, meta: dict) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO generations(email, ts, client, project) VALUES(?, ?, ?, ?)",
            (email, _iso(_utcnow()), meta.get("client", ""), meta.get("project_type", "")),
        )


# ---- email + password fallback ------------------------------------------------ #

PBKDF2_ROUNDS = 240_000
MAX_FAILS = 5
LOCK_MINUTES = 15


def _pw_hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)


def create_user(email: str, password: str) -> str | None:
    """Returns an error message, or None on success."""
    if not EMAIL_RE.match(email):
        return "That doesn't look like a valid email."
    if len(password) < 8:
        return "Use at least 8 characters for your password."
    salt = os.urandom(16)
    try:
        with db() as c:
            c.execute(
                "INSERT INTO users(email, salt, pw_hash, created) VALUES(?, ?, ?, ?)",
                (email, salt, _pw_hash(password, salt), _iso(_utcnow())),
            )
    except sqlite3.IntegrityError:
        return "An account with that email already exists. Try logging in."
    return None


def verify_user(email: str, password: str) -> str | None:
    """Returns an error message, or None when the credentials are correct."""
    generic = "Email or password didn't match."
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            _pw_hash(password, b"\x00" * 16)  # keep timing similar for unknown emails
            return generic
        until = _parse(row["locked_until"])
        if until and until > _utcnow():
            return "Too many tries. Take a short break and try again in a few minutes."
        if hmac.compare_digest(_pw_hash(password, row["salt"]), row["pw_hash"]):
            c.execute("UPDATE users SET fails = 0, locked_until = NULL WHERE email = ?", (email,))
            return None
        fails = int(row["fails"]) + 1
        if fails >= MAX_FAILS:
            lock = _iso(_utcnow() + timedelta(minutes=LOCK_MINUTES))
            c.execute("UPDATE users SET fails = 0, locked_until = ? WHERE email = ?", (lock, email))
        else:
            c.execute("UPDATE users SET fails = ? WHERE email = ?", (fails, email))
        return generic


# =========================================================================== #
# Authentication gate
# =========================================================================== #

def oidc_configured() -> bool:
    try:
        return hasattr(st, "login") and "auth" in st.secrets
    except Exception:
        return False


def oidc_provider() -> str | None:
    """None for a flat [auth] config, or 'google' when configured as [auth.google]."""
    try:
        auth = st.secrets["auth"]
        if "client_id" not in auth and "google" in auth:
            return "google"
    except Exception:
        pass
    return None


def password_login_allowed() -> bool:
    default = "false" if oidc_configured() else "true"
    return str(get_secret("ALLOW_PASSWORD_LOGIN", default)).strip().lower() in ("1", "true", "yes", "on")


def current_user() -> dict | None:
    ss = st.session_state
    if oidc_configured():
        try:
            if st.user.is_logged_in:
                email = str(getattr(st.user, "email", "") or "").strip().lower()
                verified = getattr(st.user, "email_verified", True)
                if email and verified is not False:
                    name = getattr(st.user, "name", None) or email.split("@")[0]
                    return {"email": email, "name": str(name), "method": "google"}
        except Exception:
            pass
    if password_login_allowed() and ss.get("auth_email"):
        email = ss["auth_email"]
        return {"email": email, "name": email.split("@")[0], "method": "password"}
    return None


def sign_out(method: str) -> None:
    st.session_state.clear()
    if method == "google":
        st.logout()
    else:
        st.rerun()


# =========================================================================== #
# Billing: Stripe Payment Link + server-side verification
# =========================================================================== #

def stripe_link_for(email: str) -> str:
    base = str(get_secret("STRIPE_PAYMENT_LINK", DEFAULT_STRIPE_LINK))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}prefilled_email={quote(email)}"


def _stripe_get(path: str, params: dict) -> dict:
    resp = requests.get(
        f"https://api.stripe.com/v1/{path}",
        params=params,
        headers={"Authorization": f"Bearer {get_secret('STRIPE_SECRET_KEY')}"},
        timeout=12,
    )
    resp.raise_for_status()
    return resp.json()


def stripe_has_active_sub(email: str) -> bool | None:
    """True/False when Stripe answered, None when we couldn't check (no key or network error)."""
    if not get_secret("STRIPE_SECRET_KEY"):
        return None
    try:
        customers: list[dict] = []
        try:
            q = email.replace("\\", "\\\\").replace("'", "\\'")
            customers = _stripe_get("customers/search", {"query": f"email:'{q}'", "limit": 10}).get("data", [])
        except Exception:
            customers = []
        if not customers:  # search can lag right after checkout; the list endpoint is immediate
            customers = _stripe_get("customers", {"email": email, "limit": 10}).get("data", [])
        for cus in customers:
            subs = _stripe_get("subscriptions", {"customer": cus["id"], "status": "all", "limit": 20}).get("data", [])
            if any(s.get("status") in ("active", "trialing") for s in subs):
                return True
        return False
    except Exception:
        return None


def sync_subscription(email: str, acc: dict, force: bool = False) -> dict:
    """Refresh the account's plan from Stripe when it's due (or when forced)."""
    if email in pro_emails() or not get_secret("STRIPE_SECRET_KEY"):
        return acc
    checked = _parse(acc.get("stripe_checked_at"))
    relevant = (
        force
        or acc.get("plan") == "pro"
        or int(acc.get("used", 0)) >= FREE_ATTEMPTS
        or checked is None
    )
    stale = checked is None or (_utcnow() - checked) > timedelta(minutes=30)
    if not (relevant and (force or stale)):
        return acc
    active = stripe_has_active_sub(email)
    if active is None:
        return acc  # couldn't verify; don't change anything
    with db() as c:
        c.execute(
            "UPDATE accounts SET plan = ?, stripe_checked_at = ? WHERE email = ?",
            ("pro" if active else "free", _iso(_utcnow()), email),
        )
    return get_account(email)


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
# Fun stuff: copy, logo, styling
# =========================================================================== #

TAGLINES = [
    "Measure twice. Generate once.",
    "Notes in. Contract out.",
    "Less typing. More hammering.",
    "Your voice memo, but with fine print.",
    "Turn truck-seat rambling into signed work.",
    "Fine print tough enough for steel-toes.",
]

LOADING_LINES = [
    "Sharpening pencils...",
    "Consulting the stud finder...",
    "Measuring twice...",
    "Checking for hidden rot...",
    "Haggling with the lumber yard...",
    "Drafting the fine print...",
    "Squaring up the corners...",
    "Cutting once...",
]

# Same mark as before: yellow carpenter's square with tape-measure ticks on a navy tile.
LOGO_MARK = (
    '<svg width="52" height="52" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<rect width="40" height="40" rx="9" fill="#FFFFFF" fill-opacity="0.12" stroke="#FFFFFF" stroke-opacity="0.35"/>'
    '<path d="M11 10 H15 V25 H30 V29 H11 Z" fill="#F5B700"/>'
    '<path d="M19 10 H21 V12 H19 Z M23 10 H25 V12 H23 Z M27 10 H29 V12 H27 Z" fill="#FFFFFF" fill-opacity="0.7"/>'
    "</svg>"
)
LOGO_MARK_DARK = (
    '<svg width="34" height="34" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<rect width="40" height="40" rx="9" fill="#173F73"/>'
    '<path d="M11 10 H15 V25 H30 V29 H11 Z" fill="#F5B700"/>'
    '<path d="M19 10 H21 V12 H19 Z M23 10 H25 V12 H23 Z M27 10 H29 V12 H27 Z" fill="#FFFFFF" fill-opacity="0.7"/>'
    "</svg>"
)

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&family=DM+Sans:wght@400;500;700&display=swap');

:root{
  --ink:#0F1B2D; --navy:#173F73; --blueprint:#1E4E8C; --tape:#F5B700; --tape-hot:#FFD23F;
  --orange:#FF6B35; --sky:#DCEEFF; --mint:#C9F2DC; --cream:#FFFDF6; --steel:#5A6778; --rule:#D8DDE5;
}
.stApp{
  background-color:#EAF4FF !important;
  background-image:radial-gradient(#BCD6F0 1.3px, transparent 1.3px); background-size:22px 22px;
  color:var(--ink);
}
[data-testid="stAppViewContainer"], [data-testid="stHeader"]{background:transparent !important;}
.stApp p, .stApp li, .stApp label, .stApp input, .stApp textarea, .stApp button, .stApp [data-baseweb="select"]{
  font-family:'DM Sans', -apple-system, 'Segoe UI', Roboto, sans-serif;
}
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5{
  font-family:'Bricolage Grotesque','DM Sans',sans-serif !important; color:var(--ink); letter-spacing:-0.02em;
}
#MainMenu, footer{visibility:hidden; height:0;}
.block-container{max-width:1280px; padding-top:1.2rem; padding-bottom:4rem;}

/* ---------- sticker cards ---------- */
.sticker{background:#fff; border:2.5px solid var(--ink); border-radius:16px; box-shadow:5px 5px 0 var(--ink); padding:1rem 1.2rem;}
.sticker.yellow{background:var(--tape-hot);} .sticker.mint{background:var(--mint);} .sticker.sky{background:var(--sky);}
.tilt-l{transform:rotate(-1.4deg);} .tilt-r{transform:rotate(1.2deg);} .sticker.tilt-l, .sticker.tilt-r{margin-bottom:1.1rem;}
.sticker .big{font-family:'Bricolage Grotesque',sans-serif; font-weight:800; font-size:1.25rem; margin-bottom:.15rem;}
.sticker p{margin:.2rem 0 0; font-size:.93rem; color:#1f2d40;}
.num{display:inline-flex; align-items:center; justify-content:center; width:30px; height:30px; border-radius:50%; background:var(--navy); color:#fff; font-weight:800; margin-right:.5rem;}

/* ---------- hero ---------- */
.ts-hero-wrap{border:2.5px solid var(--ink); border-radius:18px; overflow:hidden; box-shadow:6px 6px 0 var(--ink); margin-bottom:1.5rem; background:var(--navy);}
.ts-hero{
  position:relative; padding:1.5rem 1.75rem; display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap;
  background-image:linear-gradient(rgba(255,255,255,.06) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.06) 1px, transparent 1px);
  background-size:22px 22px;
}
.ts-brand{display:flex; align-items:center; gap:.9rem; position:relative; z-index:2;}
.ts-name{font-family:'Bricolage Grotesque',sans-serif; font-weight:800; font-size:2rem; color:#fff; line-height:1.05;}
.ts-line{display:inline-block; margin-top:.35rem; background:var(--tape-hot); color:var(--ink); font-weight:700; font-size:.85rem; padding:.2rem .65rem; border-radius:8px; border:2px solid var(--ink); transform:rotate(-1.5deg);}
.ts-chip{position:relative; z-index:2; background:#fff; color:var(--ink); border:2.5px solid var(--ink); border-radius:14px; padding:.45rem .9rem; font-weight:700; font-size:.9rem; box-shadow:3px 3px 0 var(--ink);}
.ts-chip.pro{background:var(--tape-hot);}
.hammers{letter-spacing:.15rem; font-size:1.25rem; line-height:1;}
.hammers .spent{filter:grayscale(1); opacity:.25;}
.hazard{height:14px; background:repeating-linear-gradient(-45deg, var(--tape) 0 14px, var(--ink) 14px 28px); border-top:2.5px solid var(--ink);}
.deco{position:absolute; font-size:1.7rem; opacity:.9; animation:bob 3.2s ease-in-out infinite; z-index:1;}
.deco.d1{left:46%; top:10%;} .deco.d2{left:62%; bottom:8%; animation-delay:.8s;} .deco.d3{left:36%; bottom:6%; animation-delay:1.6s;}
@keyframes bob{50%{transform:translateY(-7px) rotate(8deg);}}
@media (prefers-reduced-motion: reduce){.deco{animation:none;}}

/* ---------- sidebar ---------- */
[data-testid="stSidebar"]{background:var(--cream); border-right:2.5px solid var(--ink);}
.side-brand{display:flex; align-items:center; gap:.55rem; font-family:'Bricolage Grotesque',sans-serif; font-weight:800; font-size:1.25rem; margin-bottom:.6rem;}
.side-h{font-family:'Bricolage Grotesque',sans-serif; font-weight:800; font-size:1rem; margin:1.1rem 0 .2rem;}
.side-sub{color:var(--steel); font-size:.8rem; margin-bottom:.5rem; line-height:1.35;}
.whoami{background:#fff; border:2px solid var(--ink); border-radius:12px; padding:.55rem .8rem; font-size:.85rem; margin-bottom:.7rem;}
.whoami b{display:block; font-size:.95rem;}
.meter{background:#fff; border:2px solid var(--ink); border-radius:12px; padding:.6rem .8rem; margin-bottom:.6rem;}
.meter .m-t{font-weight:700; font-size:.9rem; margin-top:.2rem;}

/* ---------- form card + inputs ---------- */
[data-testid="stForm"]{background:#fff; border:2.5px solid var(--ink); border-radius:16px; box-shadow:5px 5px 0 var(--ink); padding:1.3rem 1.4rem 1.2rem;}
.card-title{font-family:'Bricolage Grotesque',sans-serif; font-weight:800; font-size:1.45rem; margin-bottom:.1rem;}
.card-sub{color:var(--steel); font-size:.9rem; margin-bottom:.8rem;}
[data-testid="stWidgetLabel"] p{font-size:.86rem; font-weight:700; color:var(--ink);}
.stTextInput input, .stTextArea textarea{background:#fff !important; color:var(--ink) !important;}
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div{
  background:#fff !important; border:2px solid var(--ink) !important; border-radius:10px !important;
}
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within{box-shadow:3px 3px 0 var(--tape) !important;}
[data-testid="stExpander"]{border:2px solid var(--ink) !important; border-radius:12px !important; background:#fff;}
[data-testid="stAlert"]{border:2px solid var(--ink); border-radius:12px;}

/* ---------- buttons (chunky, they press down) ---------- */
.stButton > button, [data-testid="stFormSubmitButton"] button, [data-testid="stDownloadButton"] button,
[data-testid^="stBaseLinkButton"], [data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-primary"]{
  border:2.5px solid var(--ink) !important; border-radius:12px !important; box-shadow:3px 3px 0 var(--ink);
  font-weight:700 !important; background:#fff; color:var(--ink) !important;
  transition:transform .08s ease, box-shadow .08s ease;
}
.stButton > button:hover, [data-testid="stFormSubmitButton"] button:hover, [data-testid="stDownloadButton"] button:hover,
[data-testid^="stBaseLinkButton"]:hover{transform:translate(-1px,-1px); box-shadow:4px 4px 0 var(--ink);}
.stButton > button:active, [data-testid="stFormSubmitButton"] button:active, [data-testid="stDownloadButton"] button:active,
[data-testid^="stBaseLinkButton"]:active{transform:translate(3px,3px); box-shadow:0 0 0 var(--ink);}
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"], button[kind="primary"],
[data-testid="stBaseLinkButton-primary"]{background:var(--tape-hot) !important;}
[data-testid="stFormSubmitButton"] button p, button[kind="primary"] p, [data-testid="stBaseLinkButton-primary"] p{color:var(--ink) !important; font-weight:800;}
[data-testid="stDownloadButton"] button{background:var(--navy) !important;}
[data-testid="stDownloadButton"] button p{color:#fff !important;}
button:disabled{opacity:.5; box-shadow:none !important; transform:none !important;}

/* ---------- tabs ---------- */
button[data-baseweb="tab"] p{font-weight:700;}
[data-baseweb="tab-highlight"]{background-color:var(--orange) !important; height:4px !important;}

/* ---------- progress "tape measure" ---------- */
.crew{margin:.2rem 0 1rem;}
.crew-msg{font-weight:700; margin-bottom:.35rem;}
.tape{height:24px; border:2.5px solid var(--ink); border-radius:999px; background:#fff; overflow:hidden; position:relative; box-shadow:3px 3px 0 var(--ink);}
.tape-fill{height:100%; background:var(--tape-hot); border-right:2.5px solid var(--ink); transition:width .3s ease;}
.tape::after{content:""; position:absolute; inset:0; pointer-events:none;
  background-image:repeating-linear-gradient(90deg, transparent 0 11px, rgba(15,27,45,.5) 11px 12px);}

/* ---------- the proposal sheet ---------- */
.paper{background:#fff; border:2.5px solid var(--ink); border-radius:6px; padding:2rem 2.3rem 2.2rem; box-shadow:8px 8px 0 var(--tape); color:#1B2433; font-size:.94rem; line-height:1.55;}
.paper .lh{display:flex; justify-content:space-between; align-items:flex-start; gap:1rem; flex-wrap:wrap; border-bottom:3px solid var(--brand); padding-bottom:.9rem; margin-bottom:1.1rem;}
.paper .lh-left{display:flex; align-items:center; gap:.9rem;}
.paper .lh-logo{max-height:56px; max-width:130px; object-fit:contain;}
.paper .lh-name{font-family:'Bricolage Grotesque',sans-serif; font-weight:800; font-size:1.45rem; color:var(--brand-text); line-height:1.15;}
.paper .lh-meta{color:var(--steel); font-size:.8rem; text-align:right; line-height:1.5;}
.paper .ghost{color:#A4AEBC; font-weight:500;}
.paper h1{font-family:'Bricolage Grotesque',sans-serif; font-size:1.4rem; margin:.2rem 0 .5rem; color:var(--ink);}
.paper h2{font-family:'Bricolage Grotesque',sans-serif; background:var(--brand); color:var(--on-brand); font-size:.98rem; padding:.45rem .75rem; margin:1.5rem 0 .6rem; border-radius:3px;}
.paper h3{font-family:'Bricolage Grotesque',sans-serif; color:var(--brand-text); font-size:1.02rem; margin:1.1rem 0 .3rem;}
.paper p{margin:.35rem 0;}
.paper ul, .paper ol{margin:.3rem 0 .6rem 1.2rem; padding:0;}
.paper li{margin:.15rem 0;}
.paper hr{border:none; border-top:1px solid var(--rule); margin:1rem 0;}
.paper mark{background:#FFF1B8; color:inherit; padding:0 .15rem; border-radius:2px;}
.paper .tbl{overflow-x:auto; margin:.6rem 0;}
.paper table{border-collapse:collapse; width:100%; font-size:.86rem;}
.paper th{background:var(--brand); color:var(--on-brand); text-align:left; padding:.45rem .6rem; font-weight:700;}
.paper td{border-bottom:1px solid var(--rule); padding:.45rem .6rem; vertical-align:top;}
.paper tbody tr:nth-child(even) td{background:#F6F8FB;}
.paper .hint{color:var(--steel); margin-bottom:1.2rem;}
.paper .skel{height:.62rem; border-radius:3px; background:#E9EDF2; margin:.6rem 0;}
.paper .caret{color:var(--brand-text); animation:blink 1s steps(2,start) infinite;}
@keyframes blink{to{visibility:hidden;}}
@media (prefers-reduced-motion: reduce){.paper .caret{animation:none;}}

/* ---------- paywall ---------- */
.paywall{border:2.5px solid var(--ink); border-radius:16px; overflow:hidden; box-shadow:5px 5px 0 var(--ink); background:#fff; margin-bottom:1rem;}
.paywall .pw-body{padding:1rem 1.3rem 1.1rem; text-align:center;}
.paywall .pw-emoji{font-size:2rem; letter-spacing:.2rem;}
.paywall .pw-emoji .spent{filter:grayscale(1); opacity:.3;}
.paywall .pw-title{font-family:'Bricolage Grotesque',sans-serif; font-weight:800; font-size:1.5rem; margin:.2rem 0;}
.paywall p{color:#2B3645; margin:.2rem 0 0;}

@media (max-width:640px){
  .paper{padding:1.2rem 1rem; box-shadow:5px 5px 0 var(--tape);} .ts-hero{padding:1.1rem;} .paper .lh-meta{text-align:left;}
  .deco{display:none;} .ts-name{font-size:1.6rem;} .tilt-l,.tilt-r{transform:none;}
}
</style>
"""


# =========================================================================== #
# UI pieces
# =========================================================================== #

def hammers_html(left: int) -> str:
    return "".join(
        '<span>🔨</span>' if i < left else '<span class="spent">🔨</span>' for i in range(FREE_ATTEMPTS)
    )


def tape_bar_html(pct: float, msg: str) -> str:
    width = max(4, min(100, int(pct * 100)))
    return (
        f'<div class="crew"><div class="crew-msg">🔧 {html.escape(msg)}</div>'
        f'<div class="tape"><div class="tape-fill" style="width:{width}%"></div></div></div>'
    )


def copy_button(text: str) -> None:
    payload = json.dumps(text).replace("</", "<\\/")
    components.html(
        f"""
        <button id="copy-btn" style="
            width:100%; padding:.62rem 1rem; border-radius:12px; border:2.5px solid {INK};
            box-shadow:3px 3px 0 {INK}; background:#ffffff; color:{INK}; font-size:.95rem; font-weight:700;
            cursor:pointer; font-family:'DM Sans',-apple-system,'Segoe UI',Roboto,sans-serif;">
            📋 Copy to clipboard
        </button>
        <script>
            const text = {payload};
            const btn = document.getElementById('copy-btn');
            const label = btn.textContent;
            function done(ok) {{
                btn.textContent = ok ? '✅ Copied!' : 'Copy failed. Use the Edit tab.';
                setTimeout(() => btn.textContent = label, 2200);
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
        height=58,
    )


def init_state() -> None:
    ss = st.session_state
    ss.setdefault("proposal_text", "")
    ss.setdefault("review_notes", "")
    ss.setdefault("meta", {})
    ss.setdefault("p_color", DEFAULT_BRAND)
    ss.setdefault("logo_b64", "")
    ss.setdefault("_logo_sig", "")
    ss.setdefault("_tagline", random.choice(TAGLINES))


def render_hero(user: dict | None, acc: dict | None) -> None:
    ss = st.session_state
    if not user:
        chip = f'<div class="ts-chip">{FREE_ATTEMPTS} free proposals. No credit card.</div>'
    elif is_pro(user["email"], acc):
        chip = '<div class="ts-chip pro">⭐ Pro: unlimited proposals</div>'
    else:
        left = attempts_left(acc)
        label = f"{left} free {'proposal' if left == 1 else 'proposals'} left"
        chip = f'<div class="ts-chip"><div class="hammers">{hammers_html(left)}</div>{label}</div>'
    st.markdown(
        '<div class="ts-hero-wrap"><div class="ts-hero">'
        '<span class="deco d1">📐</span><span class="deco d2">🪚</span><span class="deco d3">🧱</span>'
        f'<div class="ts-brand">{LOGO_MARK}<div><div class="ts-name">{APP_NAME}</div>'
        f'<div class="ts-line">{html.escape(ss.get("_tagline", TAGLINES[0]))}</div></div></div>'
        f"{chip}</div><div class=\"hazard\"></div></div>",
        unsafe_allow_html=True,
    )


def render_login() -> None:
    render_hero(None, None)
    left, right = st.columns([6, 5], gap="large")

    with left:
        st.markdown(
            '<div class="sticker yellow tilt-l"><div class="big"><span class="num">1</span>Ramble</div>'
            "<p>Dictate or paste your walkthrough notes. Typos? Tangents? Perfect.</p></div>"
            '<div class="sticker mint tilt-r"><div class="big"><span class="num">2</span>Generate</div>'
            "<p>Scope by phase, timeline, milestone payments, and rot-proof fine print.</p></div>"
            '<div class="sticker sky tilt-l"><div class="big"><span class="num">3</span>Send it</div>'
            "<p>Download a branded, multi-page contract PDF with your letterhead on every page.</p></div>",
            unsafe_allow_html=True,
        )

    with right:
        st.markdown(
            '<div class="card-title">Grab your 3 free proposals</div>'
            '<div class="card-sub">Sign in so your free tries stay yours. It only takes a second.</div>',
            unsafe_allow_html=True,
        )
        shown = False
        if oidc_configured():
            shown = True
            prov = oidc_provider()
            if prov:
                st.button("Continue with Google", type="primary", use_container_width=True,
                          on_click=st.login, args=(prov,), key="google_login")
            else:
                st.button("Continue with Google", type="primary", use_container_width=True,
                          on_click=st.login, key="google_login")
        if password_login_allowed():
            shown = True
            if oidc_configured():
                st.caption("or use an email and password")
            tab_in, tab_up = st.tabs(["Log in", "Create account"])
            with tab_in:
                with st.form("login_form"):
                    e = st.text_input("Email", key="li_email")
                    p = st.text_input("Password", type="password", key="li_pw")
                    if st.form_submit_button("Log in", type="primary"):
                        email = e.strip().lower()
                        err = verify_user(email, p)
                        if err:
                            st.error(err)
                        else:
                            st.session_state["auth_email"] = email
                            get_account(email)
                            st.rerun()
            with tab_up:
                with st.form("signup_form"):
                    e2 = st.text_input("Email", key="su_email")
                    p2 = st.text_input("Password (8+ characters)", type="password", key="su_pw")
                    p3 = st.text_input("Confirm password", type="password", key="su_pw2")
                    if st.form_submit_button("Create my account", type="primary"):
                        email = e2.strip().lower()
                        if p2 != p3:
                            st.error("Those passwords don't match.")
                        else:
                            err = create_user(email, p2)
                            if err:
                                st.error(err)
                            else:
                                st.session_state["auth_email"] = email
                                get_account(email)
                                st.rerun()
        if not shown:
            st.error("Sign-in isn't set up yet. The owner needs to add Google login to the app's secrets.")
        st.caption("Your 3 free proposals are tied to your account, so they can't be reset by refreshing the page.")


def unlock_check(email: str, acc: dict, key: str) -> None:
    """The 'I already subscribed' button: asks Stripe, then unlocks Pro if it finds a live subscription."""
    if not st.button("I've subscribed, unlock me", key=key, use_container_width=True):
        return
    if not get_secret("STRIPE_SECRET_KEY"):
        support = get_secret("SUPPORT_EMAIL")
        st.info(
            "Thanks! Your payment is safe. Automatic unlock isn't switched on yet, so we'll activate your account "
            "by hand" + (f". Email {support} if it takes more than a few hours." if support else ".")
        )
        return
    new = sync_subscription(email, acc, force=True)
    if is_pro(email, new):
        st.session_state["_toast"] = "You're Pro! Unlimited proposals unlocked. 🎉"
        st.session_state["_celebrate"] = True
        st.rerun()
    else:
        st.warning(
            f"We couldn't find an active subscription for {email} yet. Stripe can take a minute to catch up. "
            "If you paid with a different email, use the same email you signed in with."
        )


def upgrade_button(email: str, label: str = "Go Pro: unlimited proposals") -> None:
    price = get_secret("PRO_PRICE_LABEL")
    st.link_button(f"{label} ({price})" if price else label, stripe_link_for(email), type="primary",
                   use_container_width=True)


def owner_checks() -> list[tuple[str, bool, str]]:
    persistent_hint = f"Database file: {DB_PATH}. Make sure this path is on persistent storage."
    return [
        ("Google login configured", oidc_configured(), "Add an [auth] section to secrets."),
        ("Password login is off (recommended)", not password_login_allowed(),
         "Turn off with ALLOW_PASSWORD_LOGIN=false once Google login works."),
        ("Anthropic API key set", bool(get_api_key()), "Set ANTHROPIC_API_KEY."),
        ("Stripe verification key set", bool(get_secret("STRIPE_SECRET_KEY")),
         "Set STRIPE_SECRET_KEY (restricted key: read Customers + Subscriptions) to auto-unlock paying users."),
        ("Database path", True, persistent_hint),
    ]


def render_sidebar(user: dict, acc: dict) -> dict:
    ss = st.session_state
    email = user["email"]
    pro = is_pro(email, acc)

    with st.sidebar:
        st.markdown(f'<div class="side-brand">{LOGO_MARK_DARK}<span>{APP_NAME}</span></div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="whoami">Signed in as<b>{html.escape(user["name"])}</b>{html.escape(email)}</div>',
            unsafe_allow_html=True,
        )

        if pro:
            st.markdown('<div class="meter"><b>⭐ Pro plan</b><div class="m-t">Unlimited proposals</div></div>',
                        unsafe_allow_html=True)
        else:
            left = attempts_left(acc)
            st.markdown(
                f'<div class="meter"><div class="hammers">{hammers_html(left)}</div>'
                f'<div class="m-t">{left} of {FREE_ATTEMPTS} free proposals left</div></div>',
                unsafe_allow_html=True,
            )
            upgrade_button(email)
            unlock_check(email, acc, key="unlock_side")

        st.markdown('<div class="side-h">Your company branding</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="side-sub">Stamped on the letterhead, header and footer of every PDF page.</div>',
            unsafe_allow_html=True,
        )
        st.text_input("Company name", key="p_company", placeholder="Northwind Builders LLC")
        st.text_input("Phone", key="p_phone", placeholder="(555) 123-4567")
        st.text_input("Business email", key="p_email", placeholder="bids@yourcompany.com")
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

        if st.button("💾 Save branding", key="save_brand", use_container_width=True):
            save_profile(email, current_profile())
            st.toast("Branding saved. It loads automatically every time you sign in.")

        if email in admin_emails():
            with st.expander("Owner checks"):
                for label, ok, hint in owner_checks():
                    st.markdown(f"{'✅' if ok else '⚠️'} **{label}**")
                    if not ok:
                        st.caption(hint)
                    elif label == "Database path":
                        st.caption(hint)

        st.write("")
        if st.button("Log out", key="logout", use_container_width=True):
            sign_out(user["method"])

    return current_profile()


def render_output(profile: dict) -> None:
    ss = st.session_state
    meta = ss.get("meta", {})

    tab_preview, tab_edit = st.tabs(["Preview", "Edit text"])
    with tab_edit:
        st.text_area("Edit before you copy or download", key="proposal_text", height=520,
                     label_visibility="collapsed")
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
            st.download_button("📄 Download branded PDF", data=pdf_bytes, file_name=filename,
                               mime="application/pdf", use_container_width=True)
        except Exception as e:  # noqa: BLE001
            st.error(f"The PDF couldn't be built: {e}")

    if ss.get("review_notes"):
        with st.expander("🔒 Contractor review notes (private, not in the PDF)", expanded=True):
            st.markdown(ss["review_notes"].replace("$", "\\$"))

    st.caption(
        "AI-generated draft. Review scope, pricing and legal language, and have a licensed attorney in your "
        "state approve your contract terms before you send it."
    )


# =========================================================================== #
# App
# =========================================================================== #

def main() -> None:
    st.set_page_config(page_title=f"{APP_NAME} | Notes in, contract out", page_icon="🔨",
                       layout="wide", initial_sidebar_state="expanded")
    st.markdown(CSS, unsafe_allow_html=True)
    init_db()
    init_state()
    ss = st.session_state

    # ---- login wall: nothing below renders for signed-out visitors ---- #
    user = current_user()
    if not user:
        render_login()
        st.stop()

    email = user["email"]
    if ss.get("_touched") != email:
        ss["_touched"] = email
        touch_login(email)
    acc = get_account(email)

    if ss.get("_loaded_for") != email:  # load saved branding once per sign-in
        ss["_loaded_for"] = email
        saved = acc.get("profile") or {}
        for f in PROFILE_FIELDS:
            ss[f"p_{f}"] = saved.get(f, DEFAULT_BRAND if f == "color" else "")
        if not ss.get("p_email"):
            ss["p_email"] = email
        ss["logo_b64"] = saved.get("logo", "")
        ss["_logo_sig"] = ""

    if st.query_params.get("paid"):  # Stripe sends customers back here after checkout
        try:
            del st.query_params["paid"]
        except Exception:
            pass
        acc = sync_subscription(email, acc, force=True)
        if is_pro(email, acc):
            ss["_toast"] = "Payment received. You're Pro! 🎉"
            ss["_celebrate"] = True
        else:
            ss["_toast"] = "Thanks! Stripe is still confirming your payment. Tap 'I've subscribed' in a minute."
    else:
        acc = sync_subscription(email, acc)

    profile = render_sidebar(user, acc)
    if ss.pop("_celebrate", False):
        st.balloons()
    render_hero(user, acc)
    if msg := ss.pop("_toast", None):
        st.toast(msg)

    pro = is_pro(email, acc)
    exhausted = (not pro) and attempts_left(acc) == 0

    left, right = st.columns([5, 7], gap="large")

    # ---------------- left: the form ---------------- #
    with left:
        with st.form("proposal_form"):
            st.markdown(
                '<div class="card-title">Spill the job-site beans</div>'
                '<div class="card-sub">Ramble, paste, dictate. We\'ll sort it out.</div>',
                unsafe_allow_html=True,
            )
            client_name = st.text_input("Client name", placeholder="Mark and Linda Johnson")
            c1, c2 = st.columns(2)
            with c1:
                project_type = st.selectbox("Project type", PROJECT_TYPES)
            with c2:
                pricing_format = st.selectbox("Pricing format", PRICING_FORMATS)
            notes = st.text_area(
                "The job, in your own words",
                height=210,
                placeholder=("14x16 kitchen, tearing out the soffit, new shaker cabinets, quartz tops, "
                             "island, the sink moves 3 feet, floor looked spongy by the dishwasher..."),
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
            submitted = st.form_submit_button(
                "Out of free tries: go Pro to continue" if exhausted else "Build my proposal 🔨",
                type="primary", disabled=exhausted,
            )

    # ---------------- right: output ---------------- #
    with right:
        out = st.container()

    if submitted:
        errors = []
        if not client_name.strip():
            errors.append("Add the client's name.")
        if not notes.strip():
            errors.append("Add some project notes.")
        with out:
            if errors:
                for e in errors:
                    st.error(e)
            elif not get_api_key():
                st.error("This app isn't connected to Claude yet. The owner needs to set ANTHROPIC_API_KEY.")
            else:
                acc = sync_subscription(email, get_account(email))
                mode = reserve_attempt(email)  # claims the attempt BEFORE any API call
                if mode is None:
                    st.warning("You're out of free proposals. Go Pro to keep building.")
                else:
                    total = parse_budget(budget)
                    table_md = payment_table_md(payment, total) if payment in SCHEDULES else ""
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
                    status = st.empty()
                    ph = st.empty()
                    full = ""

                    def store_result(full_text: str) -> None:
                        proposal, review_notes = split_notes(full_text)
                        proposal = inject_payment_table(proposal, table_md)
                        price_display = money(total) if total is not None else (budget.strip() or "[Price to be confirmed]")
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
                        ss["proposal_text"] = proposal
                        ss["review_notes"] = review_notes
                        ss["meta"] = meta
                        log_generation(email, meta)

                    def abort(msg: str) -> None:
                        ph.empty()
                        status.empty()
                        if len(full) > 600:  # we have a real draft: keep it instead of wasting the attempt
                            store_result(full)
                            ss["_toast"] = "Connection dropped mid-draft. Check the end of this proposal before sending."
                            st.rerun()
                        if mode == "trial":
                            refund_attempt(email)
                        st.error(msg + (" That didn't use up one of your free tries." if mode == "trial" else ""))

                    try:
                        ph.markdown(paper_html("", profile, empty=True), unsafe_allow_html=True)
                        for n, chunk in enumerate(stream_proposal(client, get_model(), build_user_message(data, table_md))):
                            full += chunk
                            shown = full.split(NOTES_MARKER)[0].replace(PAYMENT_MARKER, "\n" + table_md + "\n")
                            ph.markdown(paper_html(shown, profile, streaming=True), unsafe_allow_html=True)
                            if n % 6 == 0:
                                status.markdown(
                                    tape_bar_html(min(len(full) / 8500, 0.96), LOADING_LINES[(n // 45) % len(LOADING_LINES)]),
                                    unsafe_allow_html=True,
                                )
                        ph.empty()
                        status.empty()
                        store_result(full)
                        ss["_celebrate"] = True
                        ss["_toast"] = "Proposal ready! 🎉"
                        st.rerun()
                    except AuthenticationError:
                        abort("The Anthropic API key was rejected. The owner needs to check the key.")
                    except RateLimitError:
                        abort("Claude is busy right now. Give it a moment and try again.")
                    except APIError as e:
                        abort(f"Claude hit an error: {e}.")
                    except Exception as e:  # noqa: BLE001
                        abort(f"Something went wrong: {e}.")

    # ---- always-visible right column ---- #
    acc = get_account(email)
    pro = is_pro(email, acc)
    exhausted = (not pro) and attempts_left(acc) == 0
    with out:
        if exhausted:
            st.markdown(
                '<div class="paywall"><div class="hazard" style="border-top:none;border-bottom:2.5px solid var(--ink)"></div>'
                f'<div class="pw-body"><div class="pw-emoji">{hammers_html(0)}</div>'
                f'<div class="pw-title">You swung all {FREE_ATTEMPTS} free hammers!</div>'
                "<p>Go Pro to keep turning job-site rambling into signed contracts. "
                "Your branding, your saved settings and everything you've built stay right here.</p></div></div>",
                unsafe_allow_html=True,
            )
            upgrade_button(email)
            unlock_check(email, acc, key="unlock_main")
            st.write("")
        if ss.get("proposal_text"):
            render_output(profile)
        elif not submitted:
            st.markdown(paper_html("", profile, empty=True), unsafe_allow_html=True)


if __name__ == "__main__":
    main()
