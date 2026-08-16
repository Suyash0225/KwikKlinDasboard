"""Plans + feature flags — SAB EK JAGAH. Ye file hi config hai.

Naya plan add karna = PLANS dict mein ek entry. Feature on/off karna =
us plan ke `features` set mein key daalo/hatao. Code mein kahin aur kuch
nahi badalna padta: backend gates `feature_on()` padhte hain, frontend
/api/me ke `features` array se tabs lock karta hai, quotas `ai_usage_limit`
/ `whatsapp_message_limit` se lagte hain.

Naming: internal codes purane hi hain (starter/pro/growth) taaki DB rows,
billing notes aur purane links na tootein — bikne wale naam Basic/Premium/
Business hain, aur naye naam bhi `get()` mein alias ki tarah chalte hain.

Pricing PRD v2 se (₹999 base). Do baatein jaan-boojh kar:
- **Har plan mein service agent (AI replies) hai** — WhatsApp ka jawab dena
  hi product hai; demo isi se bikta hai. Upar ke plan campaigns, reports,
  marketing-agent aur volume par bikte hain.
- **Setup fee sab par hai** (PRD §2).

Limits: `None` = unlimited.
"""

from dataclasses import dataclass, field

SETUP_FEE_INR = 1999

# Gate-able features — backend dependencies aur frontend dono yahi keys
# use karte hain. Nayi feature banao to yahan register karo.
FEATURE_KEYS = (
    "billing",          # orders/bills banana-badalna, rate card
    "inbox",            # WhatsApp inbox (threads, send, media)
    "service_agent",    # AI auto-replies + training/teach-me
    "marketing_agent",  # leads pipeline + weekly marketing suggestions
    "campaigns",        # campaigns + coupons + segments
    "reports",          # reports, CSV exports, AI-usage page
    # --- staff panel (har plan mein alag suvidha) ---
    # Har shop ka staff apne phone se login karke apna kaam dekhe. Basic
    # mein sabko ek jaisa "staff" access milta hai; upar ke plan role ke
    # hisaab se kaam baantte hain.
    "staff_panel",       # /staff login + mera kaam + Done/Ask
    "staff_roles",       # washerman / delivery alag — sirf apna kaam dikhe
    "cancel_approval",   # staff cancel maange, manager approve kare
    "cod_collection",    # delivery wala paisa collect kare, ledger update
    "order_timeline",    # poori status timeline staff/manager ko dikhe
    "staff_reports",     # kis staff ne kitna kaam kiya, TAT, COD
    "barcode_tracking",  # har order par QR/barcode tag
    "multi_branch",      # ek business, kai outlet
)


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    price_inr: int              # per month
    annual_inr: int             # 12 ke daam mein 10 mahine
    tagline: str
    # --- feature flags (gates) ---
    features: frozenset
    # --- limits (None = unlimited) ---
    max_orders_month: int | None
    max_staff: int | None                 # == staff_limit
    max_outlets: int
    max_campaign_msgs_month: int
    ai_usage_limit: int | None            # LLM calls / month (per tenant)
    # BILLABLE messages/month (utility + marketing templates). Customer
    # ke 24h window mein diye gaye jawab Meta par FREE hain — wo is
    # limit mein ginte hi nahi (services/quota.py).
    whatsapp_message_limit: int | None
    ai_budget_usd_month: float            # $ hard cap — margin ki raksha
    # --- pricing-page bullets (sirf display; gates se lena-dena nahi) ---
    sales_points: list[str] = field(default_factory=list)

    @property
    def staff_limit(self) -> int | None:
        return self.max_staff


PLANS: dict[str, Plan] = {
    "starter": Plan(
        code="starter",
        name="Basic",
        price_inr=999,
        annual_inr=9990,
        tagline="One shop, WhatsApp fully handled — everything you need to start",
        # Staff panel Basic mein bhi hai — chhoti dukaan ko bhi apne
        # aadmi ka kaam phone par dikhna chahiye. Bas role ka batwara,
        # cancel-approval, COD aur reports upar ke plan mein.
        features=frozenset({"billing", "inbox", "service_agent", "staff_panel"}),
        max_orders_month=400,
        max_staff=3,
        max_outlets=1,
        max_campaign_msgs_month=0,
        ai_usage_limit=1500,
        whatsapp_message_limit=500,
        ai_budget_usd_month=3.0,
        sales_points=[
            "Orders + bills + status on WhatsApp",
            "AI agent that replies 24x7",
            "Order from a photo of the bill",
            "Inbox — reply yourself anytime",
            "Customers, dues and expenses",
            "Staff panel — 3 tak staff apne phone par kaam dekhein",
        ],
    ),
    "pro": Plan(
        code="pro",
        name="Premium",
        price_inr=1999,
        annual_inr=19990,
        tagline="For a busy shop — unlimited orders, campaigns aur reports",
        features=frozenset({
            "billing", "inbox", "service_agent", "campaigns", "reports",
            "staff_panel", "staff_roles", "cancel_approval", "cod_collection",
            "order_timeline", "staff_reports",
        }),
        max_orders_month=None,
        max_staff=8,
        max_outlets=1,
        max_campaign_msgs_month=500,
        ai_usage_limit=5000,
        whatsapp_message_limit=2500,
        ai_budget_usd_month=8.0,
        sales_points=[
            "Everything in Basic",
            "Unlimited orders",
            "Campaigns — win back old customers",
            "Reports + GST invoices + CSV export",
            "Understands voice notes",
            "Staff panel: washerman/delivery alag, COD collection, cancel approval",
            "Up to 8 staff",
        ],
    ),
    "growth": Plan(
        code="growth",
        name="Business",
        price_inr=4999,
        annual_inr=49990,
        tagline="2-3 outlets or 50+ orders a day — built for chains",
        features=frozenset(FEATURE_KEYS),  # sab kuch on
        max_orders_month=None,
        max_staff=None,
        max_outlets=3,
        max_campaign_msgs_month=1500,
        ai_usage_limit=15000,
        whatsapp_message_limit=7500,
        ai_budget_usd_month=20.0,
        sales_points=[
            "Everything in Premium",
            "Marketing agent — leads + weekly suggestions",
            "Up to 3 outlets",
            "Priority support (on WhatsApp)",
            "Staff panel: barcode/QR tracking + branch-wise kaam",
            "Unlimited staff",
        ],
    ),
}

# Naye naam bhi chalte hain — pricing page/marketing "Basic/Premium/Business"
# bolti hai, DB codes purane hain.
ALIASES = {"basic": "starter", "premium": "pro", "business": "growth"}

DEFAULT_PLAN = "starter"
TRIAL_DAYS = 7


def get(code: str) -> Plan:
    """Plan by code ya alias; unknown -> Basic (never crash a login over it)."""
    c = (code or "").strip().lower()
    c = ALIASES.get(c, c)
    return PLANS.get(c, PLANS[DEFAULT_PLAN])


OVERRIDABLE_LIMITS = (
    "ai_usage_limit", "whatsapp_message_limit", "max_orders_month", "max_staff",
    "max_campaign_msgs_month",
)


def effective_limits(tenant) -> dict:
    """Tenant ke ASLI limits: plan defaults + vendor ke per-client overrides.

    Har enforcement point (quota, order create, staff invite) YAHI use kare
    — plan ko seedha padhna ab galat hai. Override value -1 = unlimited.
    """
    p = get(tenant.plan if tenant is not None else DEFAULT_PLAN)
    out = {
        "ai_usage_limit": p.ai_usage_limit,
        "whatsapp_message_limit": p.whatsapp_message_limit,
        "max_orders_month": p.max_orders_month,
        "max_staff": p.max_staff,
        "max_campaign_msgs_month": p.max_campaign_msgs_month,
    }
    for k, v in ((getattr(tenant, "limit_overrides", None) or {}).items()):
        if k in OVERRIDABLE_LIMITS and v is not None:
            out[k] = None if v == -1 else int(v)
    return out


def feature_on(plan_code: str, feature: str) -> bool:
    """Kya is plan mein ye feature khula hai? Gates yahi poochte hain."""
    return feature in get(plan_code).features


def plan_with_feature(feature: str) -> str | None:
    """Sabse sasta plan jisme ye feature hai — 'Upgrade to X' ke liye."""
    for code in ("starter", "pro", "growth"):
        if feature in PLANS[code].features:
            return code
    return None


def public_catalog() -> list[dict]:
    """What the pricing page shows. Prices GST ke bina hain."""
    return [
        {
            "code": p.code,
            "name": p.name,
            "price_inr": p.price_inr,
            "annual_inr": p.annual_inr,
            "annual_saving_inr": p.price_inr * 12 - p.annual_inr,
            "tagline": p.tagline,
            "features": p.sales_points,
            "flags": sorted(p.features),
            "max_orders_month": p.max_orders_month,
            "max_staff": p.max_staff,
            "max_outlets": p.max_outlets,
            "popular": p.code == "pro",
        }
        for p in PLANS.values()
    ]


class LimitReached(Exception):
    """Plan ki limit khatam. Caller isse 402 banata hai, 500 nahi."""

    def __init__(self, message: str, *, limit: str, upgrade_to: str | None = None):
        super().__init__(message)
        self.limit = limit
        self.upgrade_to = upgrade_to


def next_plan_after(code: str) -> str | None:
    order = ["starter", "pro", "growth"]
    try:
        i = order.index(get(code).code)
    except ValueError:
        return "pro"
    return order[i + 1] if i + 1 < len(order) else None


# --- Recharge packs -------------------------------------------------------
# Client apne billing page se yahi kharidta hai; vendor approve karta hai.
# Cost basis (India, Aug 2026 — apne Meta rate card se confirm karte rehna):
#   marketing template ~₹0.80 | utility ~₹0.13 | service reply ₹0 (free)
#   AI reply (Gemini Flash-Lite) ~₹0.02
# Har pack ka margin comment mein — bina margin ka pack yahan mat daalna.
RECHARGE_PACKS = [
    {"id": "wa-1000", "kind": "wa", "units": 1000, "price_inr": 1500,
     "title": "1,000 business messages",
     "detail": "Order updates, payment reminders, offers. Customer ke apne "
               "message ka jawab isme se NAHI katta — wo hamesha free hai.",
     "cost_inr": 800},          # margin ₹700
    {"id": "wa-5000", "kind": "wa", "units": 5000, "price_inr": 6000,
     "title": "5,000 business messages",
     "detail": "Bade shop / campaign ke liye — per message ₹1.20 (20% saving).",
     "cost_inr": 4000},         # margin ₹2,000
    {"id": "ai-5000", "kind": "ai", "units": 5000, "price_inr": 500,
     "title": "5,000 AI replies",
     "detail": "Agent ke extra jawab, jab mahine ki limit khatam ho jaye.",
     "cost_inr": 100},          # margin ₹400
    {"id": "ai-20000", "kind": "ai", "units": 20000, "price_inr": 1500,
     "title": "20,000 AI replies",
     "detail": "Busy shop ke liye — per reply ₹0.075 (25% saving).",
     "cost_inr": 400},          # margin ₹1,100
]


def pack(pack_id: str) -> dict | None:
    return next((p for p in RECHARGE_PACKS if p["id"] == pack_id), None)


def public_packs() -> list[dict]:
    """Client ko dikhne wala price card — cost/margin kabhi bahar nahi."""
    return [
        {k: v for k, v in p.items() if k != "cost_inr"} for p in RECHARGE_PACKS
    ]


# Cost basis (India, Aug 2026 — apne Meta rate card se confirm karte rehna).
COST_MARKETING_INR = 0.80    # per marketing template
COST_UTILITY_INR = 0.13      # per utility template
COST_SERVICE_INR = 0.0       # customer ke 24h window ka jawab — FREE
COST_AI_REPLY_INR = 0.02     # Gemini Flash-Lite, ~1.5k in / 150 out
SERVER_COST_PER_TENANT_INR = 150


def worst_case_cost(plan_code: str) -> float:
    """Ek client is plan par MAXIMUM kitna kharch kara sakta hai.

    Sabse mehnga scenario: marketing cap poora bhara, baaki billable
    messages utility, AI limit poori. Isse neeche kabhi nahi, upar isliye
    nahi kyunki dono cap enforce hote hain (quota + campaign budget).
    """
    p = get(plan_code)
    marketing = p.max_campaign_msgs_month
    billable = p.whatsapp_message_limit or 0
    utility = max(0, billable - marketing)
    return (
        marketing * COST_MARKETING_INR
        + utility * COST_UTILITY_INR
        + (p.ai_usage_limit or 0) * COST_AI_REPLY_INR
        + SERVER_COST_PER_TENANT_INR
    )


def margin(plan_code: str) -> float:
    """Worst case par bhi kitna % bachta hai."""
    p = get(plan_code)
    return (p.price_inr - worst_case_cost(plan_code)) / p.price_inr
