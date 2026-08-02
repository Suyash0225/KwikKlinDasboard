"""ALL human-facing strings live here, keyed by message key + language.

Rules:
- No other module may contain customer/staff-facing text. Ever.
- "hi" = Hinglish (Hindi in Latin script) — what our customers actually type.
- Unknown language falls back to "hi", then "en".
- Editing copy here must never require touching logic elsewhere.
- INTERNAL info (orders.notes, delay reasons) must never appear in any
  string here — customers get polite notices only.
"""

import structlog

from app.config import settings
from app.models import OrderStatus

log = structlog.get_logger()

DEFAULT_LANG = "hi"

MESSAGES: dict[str, dict[str, str]] = {
    # --- generic ---
    "ack_received": {
        "hi": "Namaste! Aapka message mil gaya 🙏 Hum jald hi jawaab denge. — {shop}",
        "en": "Hello! We received your message and will reply shortly. — {shop}",
    },
    "error_fallback": {
        "hi": "Maaf kijiye, abhi thodi dikkat aa gayi hai. Hum jald hi aapse sampark karenge. — {shop}",
        "en": "Sorry, something went wrong on our side. We will get back to you shortly. — {shop}",
    },
    # --- order lifecycle notifications ---
    "order_confirmed": {
        "hi": "Namaste! Aapka order {order_number} humein mil gaya 🧺 ({items_count} items). Ready hote hi khabar karenge. — {shop}",
        "en": "Hello! Your order {order_number} is received ({items_count} items). We'll update you when it's ready. — {shop}",
    },
    "order_confirmed_with_date": {
        "hi": "Namaste! Aapka order {order_number} humein mil gaya 🧺 ({items_count} items). Expected delivery: {date}. — {shop}",
        "en": "Hello! Your order {order_number} is received ({items_count} items). Expected delivery: {date}. — {shop}",
    },
    "order_ready": {
        "hi": "Khushkhabri! Aapka order {order_number} taiyar hai ✨ Jald hi delivery hogi. — {shop}",
        "en": "Good news! Your order {order_number} is ready ✨ Delivery soon. — {shop}",
    },
    "order_out_for_delivery": {
        "hi": "Aapka order {order_number} delivery ke liye nikal chuka hai 🛵 — {shop}",
        "en": "Your order {order_number} is out for delivery 🛵 — {shop}",
    },
    "order_delivered": {
        "hi": "Aapka order {order_number} deliver ho gaya ✅ Dhanyawad, {shop} ko mauka dene ke liye! 🙏",
        "en": "Your order {order_number} has been delivered ✅ Thank you for choosing {shop}! 🙏",
    },
    "delay_notice": {
        "hi": "Namaste, aapke order {order_number} ki expected delivery ab {date} hai. Asuvidha ke liye maafi 🙏 — {shop}",
        "en": "Hello, the expected delivery for order {order_number} is now {date}. Sorry for the inconvenience 🙏 — {shop}",
    },
    # --- rule-based status replies (webhook) ---
    "status_reply": {
        "hi": "Aapka order {order_number} {status_label}",
        "en": "Your order {order_number} {status_label}",
    },
    "status_reply_with_date": {
        "hi": "Aapka order {order_number} {status_label} Expected delivery: {date}. — {shop}",
        "en": "Your order {order_number} {status_label} Expected delivery: {date}. — {shop}",
    },
    "orders_list_header": {
        "hi": "Aapke {count} orders chal rahe hain:",
        "en": "You have {count} orders in progress:",
    },
    "order_not_found": {
        "hi": "Maaf kijiye, ye order number humein nahi mila. Number check karke dobara bhejein 🙏 — {shop}",
        "en": "Sorry, we couldn't find that order number. Please check and resend 🙏 — {shop}",
    },
}

# How each status reads in a sentence: "Aapka order KK-... <label>"
STATUS_LABELS: dict[str, dict[OrderStatus, str]] = {
    "hi": {
        OrderStatus.RECEIVED: "mil gaya hai, jald kaam shuru hoga 🧺.",
        OrderStatus.IN_WASH: "abhi dhulai mein hai 🧼.",
        OrderStatus.IN_DRY: "dhul chuka hai, sukh raha hai.",
        OrderStatus.IN_IRON: "istri ho rahi hai 👔.",
        OrderStatus.READY: "taiyar hai ✨ Jald delivery hogi.",
        OrderStatus.OUT_FOR_DELIVERY: "delivery ke liye nikal chuka hai 🛵.",
        OrderStatus.DELIVERED: "deliver ho chuka hai ✅.",
        OrderStatus.ON_HOLD: "thodi der ke liye ruka hua hai, jald shuru hoga.",
        OrderStatus.CANCELLED: "cancel ho chuka hai.",
    },
    "en": {
        OrderStatus.RECEIVED: "has been received 🧺.",
        OrderStatus.IN_WASH: "is being washed 🧼.",
        OrderStatus.IN_DRY: "is drying.",
        OrderStatus.IN_IRON: "is being ironed 👔.",
        OrderStatus.READY: "is ready ✨ Delivery soon.",
        OrderStatus.OUT_FOR_DELIVERY: "is out for delivery 🛵.",
        OrderStatus.DELIVERED: "has been delivered ✅.",
        OrderStatus.ON_HOLD: "is briefly on hold.",
        OrderStatus.CANCELLED: "has been cancelled.",
    },
}


def get_message(key: str, lang: str = DEFAULT_LANG, **fmt: str) -> str:
    """Return the string for `key` in `lang`, formatted.

    {shop} is always available in format placeholders. Raises KeyError for an
    unknown key — that is a programming error we want to hear about loudly.
    """
    try:
        by_lang = MESSAGES[key]
    except KeyError:
        log.error("unknown_message_key", key=key)
        raise
    text = by_lang.get(lang) or by_lang.get(DEFAULT_LANG) or by_lang["en"]
    fmt.setdefault("shop", settings.SHOP_NAME)
    return text.format(**fmt)


def status_label(status: OrderStatus, lang: str = DEFAULT_LANG) -> str:
    """Human wording for a status, for use after 'Aapka order X ...'."""
    by_lang = STATUS_LABELS.get(lang) or STATUS_LABELS[DEFAULT_LANG]
    return by_lang[status]
