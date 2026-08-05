"""Classify an inbound customer message's intent using the cheap model.

Returns None on any LLM failure — callers then keep today's rule-based
behavior (ground rule #5: degrade, never go silent).
"""

import structlog

from app.services import llm_client
from app.services.llm_client import LLMError

log = structlog.get_logger()

INTENTS = (
    "ORDER_STATUS",   # "kab milega", "ready hai kya", mentions an order
    "NEW_ORDER",      # wants to give clothes / book a pickup
    "PRICE_QUERY",    # "kurta ka kitna loge", rate questions
    "COMPLAINT",      # angry / problem with clothes or service
    "GREETING",       # hi / namaste / thanks — small talk
    "OTHER",          # anything else
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "language": {"type": "string", "enum": ["hi", "en"]},
    },
    "required": ["intent", "language"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You classify WhatsApp messages sent to Kwik Klin, a laundry shop in "
    "Varanasi, India. Customers write in Hindi, English, or Hinglish "
    "(Hindi in latin script). Classify the message's intent and the "
    "language the reply should use (hi = Hindi/Hinglish, en = English). "
    "COMPLAINT means the customer is unhappy or reporting a problem. "
    "If the message asks when clothes will be ready or mentions an order, "
    "it is ORDER_STATUS. If unsure, use OTHER."
)


async def classify_intent(text: str) -> dict | None:
    """Return {"intent": ..., "language": ...} or None if the LLM is down."""
    try:
        with llm_client.track("intent"):
            result = await llm_client.ask_json(
                system=_SYSTEM,
                user_text=text[:1000],
                schema=_SCHEMA,
                model=llm_client.MODEL_CHEAP,
                max_tokens=100,
            )
    except LLMError as exc:  # includes LLMUnavailable
        log.warning("intent_classify_failed", error=str(exc)[:150])
        return None
    log.info("intent_classified", intent=result["intent"], language=result["language"])
    return result
