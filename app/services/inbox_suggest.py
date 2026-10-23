"""Inbox ✨: poori chat padh kar owner ko reply ke sujhaav.

Baat hui, phir grahak chup — owner ko samajh nahi aata ab kya likhe. Ye
pichle saare message + grahak ka record (orders, baaki paise, rate card)
padh kar 2-3 reply sujhaata hai. Sujhaav sirf compose box mein aata hai;
bhejta owner khud hai — AI yahan kuch nahi bhejta.

Owner ke button dabane par hi chalta hai, isliye sabse bada model aur gehri
soch (llm_client.deep_thinking) — har message wale AI raaste se mehenga,
par ye kabhi-kabhi ka kaam hai.
"""

from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Customer, Direction
from app.services import llm_client, shop_identity

log = structlog.get_logger()

HISTORY_LIMIT = 80

_SCHEMA = {
    "type": "object",
    "properties": {
        "situation": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["label", "text"],
            },
        },
    },
    "required": ["situation", "suggestions"],
}

_SYSTEM = """You help the owner of {shop}, a laundry and dry-cleaning shop, decide what to send next on WhatsApp.

You get the full chat with one customer (oldest first) and the shop's records for that customer.
Work out where the conversation stands: what the customer wanted, what was promised, what is still open, and why they may have gone quiet (price, timing, no clear next step, an unanswered question, a complaint, an unpaid bill, or just busy).

Then write 3 different replies the owner could send now. Each must:
- move the conversation to a clear, easy next step (book a pickup, confirm a time, answer the open question, share the bill or order link);
- be short, warm and natural, like a real shopkeeper on WhatsApp; never pushy, never a sales blast;
- use the same language and script the customer writes in (Hinglish in Latin script, Hindi in Devanagari, or English);
- use only facts from the chat and records. Never invent prices, dates, discounts or offers; if a rate is needed, quote it from the rate card;
- not repeat a message the shop already sent.
Make the 3 replies different in approach (for example: a gentle check-in, answering the open point directly, a concrete pickup offer).

"label" is 2-4 English words naming the approach. "situation" is one short English sentence for the owner about where things stand.
If the customer is clearly upset, lead with an apology and a fix, not a sale."""


def _line(m: Conversation) -> str:
    who = "Customer" if m.direction is Direction.INBOUND else (
        "Shop (automatic)" if (m.sent_by or "") in ("bot", "ai", "system", "marketing") else "Shop"
    )
    at = m.created_at.astimezone(timezone.utc).strftime("%d %b %H:%M") if m.created_at else ""
    return f"[{at}] {who}: {(m.message_text or '').strip()[:1500]}"


async def suggest(db: AsyncSession, customer: Customer) -> dict:
    """{"situation", "suggestions": [{"label", "text"}]}. Raises LLMError."""
    from app.services.ai_agent import _build_facts

    msgs = (
        await db.execute(
            select(Conversation)
            .where(Conversation.customer_id == customer.id)
            .order_by(Conversation.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
    ).scalars().all()
    msgs = [m for m in reversed(msgs) if (m.message_text or "").strip()]
    if not msgs:
        return {"situation": "No messages in this chat yet.", "suggestions": []}

    shop = (await shop_identity.current(db)).name
    facts = await _build_facts(db, customer)
    quiet = ""
    last = msgs[-1]
    if last.created_at:
        days = (datetime.now(timezone.utc) - last.created_at).days
        side = "customer" if last.direction is Direction.INBOUND else "shop"
        quiet = f"Last message was from the {side}, {days} day(s) ago.\n"

    user_text = (
        f"SHOP RECORDS FOR THIS CUSTOMER:\n{facts}\n\n"
        f"{quiet}CHAT (oldest first):\n" + "\n".join(_line(m) for m in msgs)
    )
    with llm_client.track("inbox_suggest"), llm_client.attribution(customer_id=customer.id), \
            llm_client.deep_thinking():
        out = await llm_client.ask_json(
            system=_SYSTEM.format(shop=shop),
            user_text=user_text,
            schema=_SCHEMA,
            model=llm_client.MODEL_SMART,
            max_tokens=1500,
        )
    suggestions = [
        {"label": str(s.get("label") or "").strip()[:40], "text": str(s.get("text") or "").strip()[:1500]}
        for s in (out.get("suggestions") or [])
        if isinstance(s, dict) and str(s.get("text") or "").strip()
    ][:3]
    return {"situation": str(out.get("situation") or "").strip()[:300], "suggestions": suggestions}
