""" TradeScope AI — proposal and contract generator for residential contractors.
#### Setup
pip install streamlit>=1.40 anthropic reportlab pillow
export ANTHROPIC_API_KEY="sk-ant-..."         # or .streamlit/secrets.toml     streamlit run tradescope_ai.py
Recommended .streamlit/config.toml (the UI is designed for the light theme):
[theme]     base = "light"     primaryColor = "#1E4E8C"
#### Optional secrets / environment variables
TRADESCOPE_MODEL        Claude model override (default below)     DEMO_BILLING            "true" (default) shows a one-click demo upgrade; set "false" in production     STRIPE_PAYMENT_LINK     Your Stripe Payment Link; shows a "Subscribe" button     PRO_ACTIVATION_CODES    Comma-separated codes that unlock Pro (e.g. issued after checkout)     TRADESCOPE_DATA_DIR     Where accounts.json is stored (default ./.tradescope_data)
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
from decimal import Decimal, ROUND_HALF_UP
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

# ===========================================================================
# Configuration
# ===========================================================================
APP_NAME = "TradeScope AI"
DEFAULT_MODEL = "claude-sonnet-5-5"
NOTES_MARKER = "<<<CONTRACTOR_NOTES>>>"
PAYMENT_MARKER = "[[PAYMENT_TABLE]]"

FREE_TRIAL_LIMIT = 3
# Changed subscription price to legitimate $20 per month
PRO_PRICE_LABEL = "$20 per month"
DEFAULT_STRIPE_LINK = "https://buy.stripe.com/5kQeVe2e0bWk9yQ8Q2es000"

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

SCHEDULES = {
    "30/40/30 (Standard)": [(30, "Deposit", "At signing"), (40, "Progress", "At mid-point"), (30, "Final", "Upon completion")],
    "50/50 (Small Jobs)": [(50, "Deposit", "At signing"), (50, "Final", "Upon completion")],
    "10/40/40/10 (Large Commercial)": [(10, "Deposit", "At signing"), (40, "Phase 1", "Framing"), (40, "Phase 2", "Finishes"), (10, "Final", "Completion")],
}

# --- PAGE CONFIG ---
st.set_page_config(page_title=APP_NAME, page_icon="🏗️", layout="wide")

# --- CUSTOM CSS FIX (Fixes white-on-white text issues in subscription modal) ---
st.markdown("""
    <style>
    /* Upgrade Modal Pricing Card Fixes */
    .plan-card {
        background-color: #ffffff !important;
        border: 1px solid #d8dde5 !important;
        border-radius: 8px !important;
        padding: 20px !important;
        color: #0F1B2D !important;
    }
    .plan-title {
        color: #0F1B2D !important;
        font-weight: 700 !important;
        font-size: 1.25rem !important;
    }
    .plan-price {
        color: #0F1B2D !important;
        font-size: 2.2rem !important;
        font-weight: 800 !important;
    }
    .plan-price-period {
        color: #5A6778 !important;
        font-size: 0.95rem !important;
    }
    .plan-features {
        color: #334155 !important;
    }
    </style>
""", unsafe_allow_html=True)

# Helper functions
def safe_hex(value: str | None) -> str:
    return value if value and re.fullmatch(r"#[0-9a-fA-F]{6}", value) else DEFAULT_BRAND

def current_profile() -> dict:
    ss = st.session_state
    prof = {f: str(ss.get(f"p_{f}", "") or "").strip() for f in PROFILE_FIELDS}
    prof["color"] = safe_hex(ss.get("p_color"))
    prof["logo"] = ss.get("logo_b64", "") or ""
    return prof

def parse_budget(text: str) -> Decimal | None:
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
                amt = (total * Decimal(pct) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                running += amt
            else:
                amt = total - running
            amounts.append(money(amt))
    else:
        amounts = ["[ $TBD ]"] * len(rows)

    lines = ["| Milestone | % | Amount | Due |", "|---|---|---|---|"]
    for (pct, name, due), amt in zip(rows, amounts):
        lines.append(f"| {name} | {pct}% | {amt} | {due} |")
    lines.append(f"| **Total** | **100%** | **{money(total) if total is not None else '[$ TOTAL]'}** | |")
    return "\n".join(lines)

# Session State Initialization
if "generations_left" not in st.session_state:
    st.session_state.generations_left = FREE_TRIAL_LIMIT

# Sidebar Setup
st.sidebar.title(f"🏗️ {APP_NAME}")
st.sidebar.header("💳 Subscription Plan")
st.sidebar.write(f"Remaining Free Proposals: **{st.session_state.generations_left} / {FREE_TRIAL_LIMIT}**")

# Upgrade Modal / Section
stripe_link = st.secrets.get("STRIPE_PAYMENT_LINK", DEFAULT_STRIPE_LINK)

if st.sidebar.button("Upgrade to Pro"):
    st.markdown("### Choose your plan")
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown(f"""
        <div class="plan-card">
            <div class="plan-title" style="color:#0F1B2D;">Free Trial</div>
            <div class="plan-price" style="color:#0F1B2D;">$0 <span class="plan-price-period">for 3 proposals</span></div>
            <ul class="plan-features">
                <li>Full AI proposals</li>
                <li>Branded PDF contracts</li>
                <li>Copy and edit before sending</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)

    with col2:
        st.markdown(f"""
        <div class="plan-card">
            <div class="plan-title" style="color:#0F1B2D;">Pro</div>
            <div class="plan-price" style="color:#0F1B2D;">$20 <span class="plan-price-period">per month</span></div>
            <ul class="plan-features">
                <li>Unlimited proposals</li>
                <li>Everything in the trial</li>
                <li>Priority access to new features</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)
        
    st.markdown(f"""
    <div style="margin-top: 15px;">
        <a href="{stripe_link}" target="_blank">
            <button style="background-color:#E11D48;color:white;padding:12px 24px;border:none;border-radius:6px;font-size:16px;font-weight:bold;cursor:pointer;width:100%;">
                Subscribe with card ($20/mo)
            </button>
        </a>
    </div>
    """, unsafe_allow_html=True)

st.sidebar.divider()

# Profile Inputs
st.sidebar.header("Contractor Details")
company_name = st.sidebar.text_input("Company Name", value="Apex Construction LLC", key="p_company")
phone_number = st.sidebar.text_input("Phone Number", value="(555) 019-2834", key="p_phone")
license_num = st.sidebar.text_input("License #", value="ROC #304921", key="p_license")

# Main Interface
st.title(f"🏗️ {APP_NAME}")
st.subheader("Generate Professional Estimates & Scopes of Work")

client_name = st.text_input("Client Name", "John & Mary Doe")
project_type = st.selectbox("Project Type", PROJECT_TYPES)
pricing_structure = st.selectbox("Pricing Format", PRICING_FORMATS)
payment_sched = st.selectbox("Payment Schedule", list(SCHEDULES.keys()))
budget_input = st.text_input("Estimated Budget", "$25,000")
raw_notes = st.text_area("Job Walk-Through Notes / Scope Details", placeholder="E.g., Complete kitchen overhaul, white shaker cabinets, quartz countertops...")

# Generate Action with User Feedback (Spinner/Loader)
if st.button("Generate Proposal", type="primary"):
    if st.session_state.generations_left <= 0:
        st.error(f"You have used all {FREE_TRIAL_LIMIT} free trial proposals. Please upgrade to Pro for $20/month to continue.")
    elif not raw_notes.strip():
        st.warning("Please provide initial job notes before generating a proposal.")
    else:
        api_key = st.secrets.get("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
        if not api_key:
            st.error("Missing ANTHROPIC_API_KEY. Please provide a valid Anthropic API key in secrets.")
        else:
            client = Anthropic(api_key=api_key)
            
            # Show visible progress message to the user while generating
            with st.spinner("⏳ TradeScope AI is drafting your customized project proposal... Please wait a moment."):
                try:
                    parsed_amt = parse_budget(budget_input)
                    pay_table = payment_table_md(payment_sched, parsed_amt)
                    
                    system_prompt = f"""
                    You are TradeScope AI, an expert trade contractor and cost estimator.
                    Generate a full, formal contractor proposal for client {client_name} regarding a {project_type}.
                    Contractor Info: {company_name} | {phone_number} | {license_num}.
                    Format Structure:
                    1. Executive Scope of Work
                    2. Phase Breakdown
                    3. Milestone Timeline
                    4. Payment Terms & Schedule
                    5. Change Order Rules & Exclusions (hidden rot, code updates, mold)
                    """
                    
                    response = client.messages.create(
                        model=st.secrets.get("TRADESCOPE_MODEL", DEFAULT_MODEL),
                        max_tokens=3000,
                        system=system_prompt,
                        messages=[{"role": "user", "content": f"Notes: {raw_notes}\nPricing structure: {pricing_structure}"}]
                    )
                    
                    proposal_out = response.content[0].text
                    proposal_out += "\n\n### Payment Schedule\n" + pay_table
                    
                    st.session_state.generations_left -= 1
                    st.session_state.current_proposal = proposal_out
                    
                    st.success("✅ Proposal successfully generated!")
                    st.markdown(proposal_out)
                    
                except Exception as e:
                    st.error(f"Failed to generate proposal: {e}")
