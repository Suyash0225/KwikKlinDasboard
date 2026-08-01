"""ALL human-facing strings live here, keyed by message key + language.

Rules:
- No other module may contain customer/staff-facing text. Ever.
- "hi" = Hinglish (Hindi in Latin script) — what our customers actually type.
- Unknown language falls back to "hi", then "en".
- Editing copy here must never require touching logic elsewhere.

Usage:
    from app.services.messages import get_message
    text = get_message("ack_received", lang="hi")
"""

import structlog

from app.config import settings

log = structlog.get_logger()

DEFAULT_LANG = "hi"

MESSAGES: dict[str, dict[str, str]] = {
    # Placeholder ack until Phase 3/4 provide real replies.
    "ack_received": {
        "hi": "Namaste! Aapka message mil gaya 🙏 Hum jald hi jawaab denge. — {shop}",
        "en": "Hello! We received your message and will reply shortly. — {shop}",
    },
    # Sent when something went wrong internally — the customer must never
    # see a raw error or silence.
    "error_fallback": {
        "hi": "Maaf kijiye, abhi thodi dikkat aa gayi hai. Hum jald hi aapse sampark karenge. — {shop}",
        "en": "Sorry, something went wrong on our side. We will get back to you shortly. — {shop}",
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
