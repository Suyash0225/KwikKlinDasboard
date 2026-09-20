"""WhatsApp template registry — the only list of templates we may send.

WhatsApp only delivers PRE-APPROVED templates outside the 24h window. If a
name isn't registered here (and approved in WhatsApp Manager), sending it is
a bug — build_template raises immediately instead of letting Meta reject it.

Adding a template later:
1. Create + get it approved in WhatsApp Manager
2. Add one entry to TEMPLATES below with the same name/language/param count
"""

import structlog

from app.config import settings

log = structlog.get_logger()

# Template ke "View bill" button ka domain. Meta ko template banate waqt
# pakka URL chahiye; sirf aakhri hissa (bill token) har message mein badalta
# hai. Yahi domain bill_link.url() bhi deta hai (SITE_URL).
BILL_URL_BASE = (settings.SITE_URL or "https://kwikklin.online").rstrip("/")
BILL_BUTTON = {"type": "URL", "text": "View bill", "url": f"{BILL_URL_BASE}/b/{{{{1}}}}", "sample": "AbCdEf.GhIjKl"}

# name -> {language code, number of body parameters}
# The kk_* templates must be created + approved in WhatsApp Manager with
# EXACTLY these names, languages, and {{n}} parameter counts (drafts in
# README/Phase 3 notes). Until Meta approves one, sending it fails with a
# 4xx which notify-code logs and survives.
TEMPLATES: dict[str, dict] = {
    # Meta's built-in sample template on every test number. Zero params.
    "hello_world": {"language": "en_US", "param_count": 0},
    # {{1}} = order number
    "kk_order_ready": {"language": "en_US", "param_count": 1, "url_button": True},
    "kk_order_out_for_delivery": {"language": "en_US", "param_count": 1},
    "kk_order_delivered": {"language": "en_US", "param_count": 1},
    # {{1}} = order number, {{2}} = new date
    "kk_delay_notice": {"language": "en_US", "param_count": 2},
    # {{1}} = the update text (staff/manager alerts outside the 24h window)
    "kk_staff_alert": {"language": "en_US", "param_count": 1},
    # url_button: template par "View bill" button, jiska aakhri hissa (bill
    # token) har message mein alag — order_service._notify_customer khud
    # bharta hai. Grahak ko bill/pay page ek tap par, template se bhi.
    # {{1}} name {{2}} order {{3}} items {{4}} total {{5}} advance {{6}} due {{7}} delivery
    "kk_bill_details": {"language": "en_US", "param_count": 7, "url_button": True},
    # {{1}} = order number (has rating quick-reply buttons)
    "kk_thankyou_rating": {"language": "en_US", "param_count": 1},
    # {{1}} order {{2}} delivery date
    "kk_picked_up": {"language": "en_US", "param_count": 2},
    # 24h window band hone par in do ke liye bhi Meta template chahiye —
    # approve hone tak bhejna fail hota hai aur sirf log hota hai.
    # {{1}} amount {{2}} order {{3}} balance line ("Balance due: ₹200" / "fully paid")
    "kk_payment_received": {"language": "en_US", "param_count": 3, "url_button": True},
    "kk_bill_requested": {"language": "en_US", "param_count": 3, "url_button": True},
    "kk_payment_reminder": {"language": "en_US", "param_count": 3, "url_button": True},
    # {{1}} order {{2}} delivered now {{3}} still pending
    "kk_partial_delivery": {"language": "en_US", "param_count": 3, "url_button": True},
}


# Har grahak-message ke do roop:
#   1. messages.py — free text: phone se wa.me par, ya API par 24h window khuli ho
#   2. yahan ka approved template — API par window band ho to apne aap yahi jaata hai
# Neeche har template ka poora text hai, taaki dashboard ek click mein Meta
# ko approval ke liye bhej sake (POST /agent/templates/standard). {{n}} ka
# kram upar TEMPLATES ke comment aur order_service ke params se milta hai —
# ek badle to doosra bhi badlo (test_templates_spec yahi pakadta hai).
STANDARD_SPECS: dict[str, dict] = {
    "kk_bill_details": {
        "purpose": "New bill / pickup booked",
        "body": (
            "Hello {{1}}, your laundry order {{2}} is booked.\n\n"
            "Items: {{3}}\nTotal: ₹{{4}}\nAdvance paid: ₹{{5}}\nBalance due: ₹{{6}}\n"
            "Expected delivery: {{7}}\n\nThank you for choosing us."
        ),
        "samples": ["Rahul", "KK-20260916-01", "3 Shirt, 2 Trouser", "250", "100", "150", "18 Sep"],
        "buttons": [BILL_BUTTON],
    },
    "kk_picked_up": {
        "purpose": "Clothes picked up",
        "body": (
            "Your clothes for order {{1}} have been picked up. "
            "Expected delivery: {{2}}. We will update you when they are ready."
        ),
        "samples": ["KK-20260916-01", "18 Sep"],
    },
    "kk_order_ready": {
        "purpose": "Order ready",
        "body": "Good news! Your laundry order {{1}} is ready. Reply to this message to schedule delivery.",
        "samples": ["KK-20260916-01"],
        "buttons": [BILL_BUTTON],
    },
    "kk_order_out_for_delivery": {
        "purpose": "Out for delivery",
        "body": "Your laundry order {{1}} is out for delivery and will reach you soon.",
        "samples": ["KK-20260916-01"],
    },
    "kk_delay_notice": {
        "purpose": "Delivery delayed",
        "body": (
            "Sorry, your laundry order {{1}} is delayed. "
            "The new expected delivery date is {{2}}. Thank you for your patience."
        ),
        "samples": ["KK-20260916-01", "20 Sep"],
    },
    "kk_payment_received": {
        "purpose": "Payment received",
        "body": "Thank you! We received ₹{{1}} for order {{2}}.\n{{3}}\nWe appreciate your business.",
        "samples": ["150", "KK-20260916-01", "Your bill is fully paid"],
        "buttons": [BILL_BUTTON],
    },
    "kk_partial_delivery": {
        "purpose": "Part of the order delivered",
        "body": (
            "Part of your laundry order {{1}} has been delivered: {{2}} pieces today. "
            "The remaining {{3}} pieces will be delivered soon."
        ),
        "samples": ["KK-20260916-01", "4", "2"],
        "buttons": [BILL_BUTTON],
    },
    "kk_thankyou_rating": {
        "purpose": "Delivered + rating buttons",
        "body": (
            "Your laundry order {{1}} has been delivered. Thank you! "
            "How was our service? Tap a button below."
        ),
        "samples": ["KK-20260916-01"],
        # webhook.py inhi texts se rating pehchanta hai — badalna mat
        "buttons": [
            {"type": "QUICK_REPLY", "text": "⭐ Excellent"},
            {"type": "QUICK_REPLY", "text": "🙂 It was okay"},
            {"type": "QUICK_REPLY", "text": "😞 Needs work"},
        ],
    },
    "kk_bill_requested": {
        "purpose": "Customer asked for bill",
        "body": "Here is your bill for order {{1}}. Total: ₹{{2}}. Balance due: ₹{{3}}.",
        "samples": ["KK-20260916-01", "250", "150"],
        "buttons": [BILL_BUTTON],
    },
    "kk_payment_reminder": {
        "purpose": "Payment reminder",
        "body": "Reminder: ₹{{1}} is pending for order {{2}}. Please pay when convenient.",
        "samples": ["150", "KK-20260916-01"],
        "buttons": [BILL_BUTTON],
    },
    "kk_staff_alert": {
        "purpose": "Staff / owner alert",
        "body": "Kwik Klin update: {{1}}. Reply here to see details.",
        "samples": ["New pickup assigned: Sector 21, 5 PM"],
    },
}


def clean_param(p: str) -> str:
    """Meta template variable mein newline/tab/4+ spaces par send reject karta hai."""
    import re

    s = re.sub(r"\s*[\r\n\t]+\s*", " · ", str(p or "")).strip(" ·")
    return re.sub(r" {2,}", " ", s) or "-"


# Templates created from the dashboard's Template Studio register here at
# runtime once Meta approves them (refreshed on every studio page load).
_DYNAMIC: dict[str, dict] = {}


def register_dynamic(name: str, language: str, param_count: int) -> None:
    if name not in TEMPLATES:
        _DYNAMIC[name] = {"language": language, "param_count": param_count}


def build_template(name: str, params: list[str] | None = None, url_param: str | None = None) -> dict:
    """Build the `template` object for the Graph API send payload.

    url_param: "View bill" button ka badalne wala hissa (bill token) — sirf
    un templates par jinke registry mein url_button hai.
    Raises ValueError for unknown template or wrong parameter count.
    """
    registry = {**_DYNAMIC, **TEMPLATES}
    if name not in registry:
        log.error("unknown_template", template=name, known=list(registry))
        raise ValueError(f"template {name!r} is not registered in templates.py")

    spec = registry[name]
    params = params or []
    if len(params) != spec["param_count"]:
        raise ValueError(
            f"template {name!r} needs {spec['param_count']} params, got {len(params)}"
        )

    payload: dict = {"name": name, "language": {"code": spec["language"]}}
    components: list[dict] = []
    if params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": clean_param(p)} for p in params],
        })
    if spec.get("url_button") and url_param:
        components.append({
            "type": "button", "sub_type": "url", "index": "0",
            "parameters": [{"type": "text", "text": url_param}],
        })
    if components:
        payload["components"] = components
    return payload
