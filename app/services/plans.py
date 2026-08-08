"""Kya bech rahe hain — plans, prices aur unki limits, ek jagah.

Pricing PRD v2 se (₹999 base, volume play). Do baatein jaan-boojh kar aisi
hain:

- **Har plan mein AI hai.** WhatsApp ka jawab dena hi product hai; usse
  paywall ke peeche rakhne se demo hi mar jaata hai. Upar ke plan multi-
  outlet, campaigns aur volume par bikte hain, "AI on/off" par nahi.
- **Setup fee sab par hai.** Is segment ka leader software free deta hai aur
  onboarding par ₹5,000 leta hai (PRD §2) — paisa wahin hai, aur jo setup ka
  paisa deta hai wo product use bhi karta hai.

Limits `enforce()` se lagti hain. `None` = unlimited.
"""

from dataclasses import dataclass, field

# One-time, sab plans par. Isi mein WhatsApp connect + rate card entry +
# staff setup + training aata hai.
SETUP_FEE_INR = 1999


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    price_inr: int              # per month
    annual_inr: int             # 12 ke daam mein 10 mahine
    tagline: str
    max_orders_month: int | None
    max_staff: int | None
    max_outlets: int
    max_campaign_msgs_month: int
    ai_budget_usd_month: float  # LLM ka hard cap — margin ki raksha
    features: list[str] = field(default_factory=list)


PLANS: dict[str, Plan] = {
    "starter": Plan(
        code="starter",
        name="Starter",
        price_inr=999,
        annual_inr=9990,
        tagline="Ek dukaan, poora WhatsApp — shuru karne ke liye kaafi",
        max_orders_month=400,
        max_staff=3,
        max_outlets=1,
        max_campaign_msgs_month=200,
        ai_budget_usd_month=3.0,
        features=[
            "WhatsApp par order + bill + status",
            "AI jawab dene wala agent (24x7)",
            "Bill ki photo se order",
            "Inbox — khud reply karo",
            "Customers, udhaar, expenses",
            "Staff ko kaam + follow-up",
            "3 staff tak",
        ],
    ),
    "pro": Plan(
        code="pro",
        name="Pro",
        price_inr=1999,
        annual_inr=19990,
        tagline="Chalti hui dukaan ke liye — unlimited order + marketing",
        max_orders_month=None,
        max_staff=8,
        max_outlets=1,
        max_campaign_msgs_month=2000,
        ai_budget_usd_month=8.0,
        features=[
            "Starter ka sab kuch",
            "Unlimited orders",
            "Campaigns — purane customer wapas laao",
            "Voice note samajhna",
            "Reports + GST invoice",
            "Google review automation",
            "8 staff tak",
        ],
    ),
    "growth": Plan(
        code="growth",
        name="Growth",
        price_inr=3999,
        annual_inr=39990,
        tagline="2-3 outlet ya roz 50+ order — chain chalane ke liye",
        max_orders_month=None,
        max_staff=None,
        max_outlets=3,
        max_campaign_msgs_month=8000,
        ai_budget_usd_month=20.0,
        features=[
            "Pro ka sab kuch",
            "3 outlet tak",
            "Delivery route planning",
            "Area benchmark report",
            "Priority support (WhatsApp par)",
            "Unlimited staff",
        ],
    ),
}

DEFAULT_PLAN = "starter"
TRIAL_DAYS = 14


def get(code: str) -> Plan:
    """Plan by code; unknown code -> Starter (never crash a login over it)."""
    return PLANS.get((code or "").strip().lower(), PLANS[DEFAULT_PLAN])


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
            "features": p.features,
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
        i = order.index((code or "").lower())
    except ValueError:
        return "pro"
    return order[i + 1] if i + 1 < len(order) else None
