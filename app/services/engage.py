"""Keep a live customer conversation alive — and turn it into an order.

Owner's brief (06 Aug): "jab kisi customer se baat shuru ho to har 5-6
ghante koi na koi message bhej ke reply lo taki window open rahe... aise
agent ki tarah jo customer se pyar se order nikalwa sake."

Why this module is careful about that:

WhatsApp's 24h service window reopens ONLY when the CUSTOMER writes. We
cannot hold it open by talking at them — and a shop that pings a silent
person every 6 hours collects blocks, which drags the WABA quality rating
down and eventually gets the number banned. So the rule here is:

- a follow-up goes out only while the window is still open and the thread
  is UNFINISHED (they asked something, no order came of it yet),
- at most `engage_max_followups` per conversation (default 2), spaced by
  `engage_gap_hours` (default 6),
- never in quiet hours, never to someone who opted out,
- the counter resets the moment they reply — a live conversation is never
  the thing being throttled.

That keeps the window open where it can actually be kept open (they reply),
and stops dead where continuing would only cost the shop its number.
"""

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select

from app.config import settings
from app.database import async_session_factory
from app.models import Conversation, Customer, Direction, Order
from app.services import app_settings, llm_client
from app.services.llm_client import LLMError
from app.services.order_service import ACTIVE_STATUSES
from app.services.whatsapp import SendError, WindowClosedError, send_message

log = structlog.get_logger()

IST = timezone(timedelta(hours=5, minutes=30))

# Marks our own nudges in the conversation log, so we can count them and so
# the Inbox shows who wrote what.
SENT_BY = "followup"

# The window is 24h; stop early enough that a reply still lands inside it.
_WINDOW_HOURS = 24
_STOP_BEFORE_CLOSE_H = 1


def _in_quiet_hours(now_ist: datetime) -> bool:
    start, end = settings.QUIET_HOURS_START, settings.QUIET_HOURS_END
    h = now_ist.hour
    if start > end:  # wraps midnight
        return h >= start or h < end
    return start <= h < end


_SYSTEM = (
    "You are the WhatsApp assistant of Kwik Klin, a laundry + dry-clean shop "
    "in Varanasi. A customer wrote to us a few hours ago and the conversation "
    "went quiet. Write ONE short follow-up message to them.\n"
    "Goal: be genuinely useful and warm so they reply — and, if it fits, so "
    "they place an order (pickup se lekar delivery tak sab hum karte hain).\n"
    "Rules:\n"
    "- Hinglish in Latin script, the way a friendly shop owner in Varanasi "
    "writes. Max 2 short lines. One emoji at most.\n"
    "- Pick up THEIR thread: answer or acknowledge what they actually asked, "
    "then offer the next step (pickup aaj/kal, rate bata dun, order bana dun).\n"
    "- End with a light question so replying is easy.\n"
    "- NEVER invent prices, offers, discounts or delivery dates. If you do "
    "not know a number, offer to find out.\n"
    "- No pressure, no guilt, no 'aap reply nahi kar rahe'. If they seem "
    "done, a warm 'kabhi bhi bata dijiyega' is enough.\n"
    "- Plain text only: no markdown, no ** or ##.\n"
    "Sign off with: — Kwik Klin"
)

# Used when the LLM is down. Deliberately generic — a wrong specific is
# worse than a plain one.
_FALLBACK = [
    "Namaste{name} 🙏 Aapke message ka dhyan hai — kuch aur batana ho to "
    "bataiye. Pickup aaj karva dun?\n— Kwik Klin",
    "Namaste{name}! Kapde ready hon to bata dijiye, hum ghar se le jayenge "
    "aur dhulwa ke wapas de denge. Kab bhejein?\n— Kwik Klin",
]


async def _compose(db, customer: Customer, history: str, attempt: int) -> str:
    """Warm, specific-to-them nudge. Falls back to a plain line if AI is down."""
    name = f" {customer.name.split()[0]}" if customer.name else ""
    try:
        from app.services.knowledge import knowledge_block, relevant_knowledge

        faqs, corrections, chunks = await relevant_knowledge(
            db, history[-500:] or "laundry pickup rate", audience="customer"
        )
        facts = knowledge_block(faqs, corrections, chunks)
    except Exception:
        log.exception("engage_knowledge_failed")
        facts = ""
    try:
        with llm_client.track("followup"):
            text = await llm_client.ask(
                system=_SYSTEM,
                user_text=(
                    f"SHOP FACTS (only these are true):\n{facts or '(none)'}\n\n"
                    f"CUSTOMER: {customer.name or 'naam nahi pata'}\n"
                    f"CONVERSATION SO FAR:\n{history or '(unka pehla message)'}\n\n"
                    f"This is follow-up number {attempt}. Write the message."
                ),
                model=llm_client.MODEL_SMART,
                max_tokens=220,
            )
        text = (text or "").strip()
        if text:
            return text[:900]
    except LLMError as exc:
        log.info("engage_compose_failed", error=str(exc)[:120])
    return _FALLBACK[(attempt - 1) % len(_FALLBACK)].format(name=name)


async def run_conversation_followups() -> int:
    """Hourly: nudge live-but-quiet conversations. Returns messages sent."""
    now = datetime.now(timezone.utc)
    now_ist = datetime.now(IST)
    if _in_quiet_hours(now_ist):
        return 0

    sent = 0
    async with async_session_factory() as db:
        if not bool(await app_settings.get(db, "engage_followups_enabled")):
            return 0
        gap_h = float(await app_settings.get(db, "engage_gap_hours"))
        max_n = int(await app_settings.get(db, "engage_max_followups"))
        if gap_h <= 0 or max_n <= 0:
            return 0

        # Everyone whose window is still open, oldest conversation first.
        window_start = now - timedelta(hours=_WINDOW_HOURS - _STOP_BEFORE_CLOSE_H)
        rows = (
            (
                await db.execute(
                    select(Customer)
                    .where(
                        Customer.last_message_at.isnot(None),
                        Customer.last_message_at >= window_start,
                        Customer.last_message_at <= now - timedelta(hours=gap_h),
                        Customer.opted_out.is_(False),
                        Customer.is_active.is_(True),
                    )
                    .order_by(Customer.last_message_at)
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )

        for cust in rows:
            try:
                if await _skip(db, cust, now, gap_h, max_n):
                    continue
                history = await _history(db, cust)
                attempt = await _followups_since_reply(db, cust) + 1
                text = await _compose(db, cust, history, attempt)
                await send_message(db, to_phone=cust.phone, text=text, sent_by=SENT_BY)
                sent += 1
                log.info(
                    "engage_followup_sent",
                    phone=cust.phone, attempt=attempt, chars=len(text),
                )
            except WindowClosedError:
                # window shut between the query and the send — that is fine,
                # a template blast here is exactly what we must NOT do
                log.info("engage_window_closed", phone=cust.phone)
            except SendError as exc:
                log.info("engage_send_failed", phone=cust.phone, error=str(exc)[:80])
            except Exception:
                log.exception("engage_followup_failed", phone=cust.phone)

    if sent:
        log.info("engage_followups_done", sent=sent)
    return sent


async def _skip(db, cust: Customer, now, gap_h: float, max_n: int) -> bool:
    """Everything that makes a nudge the wrong move right now."""
    # 1. an order already came of this conversation -> nothing to chase
    ordered = (
        await db.execute(
            select(func.count())
            .select_from(Order)
            .where(
                Order.customer_id == cust.id,
                Order.status.in_(ACTIVE_STATUSES),
                Order.created_at >= cust.last_message_at,
            )
        )
    ).scalar_one()
    if ordered:
        return True

    # 2. the owner is handling this thread himself (Inbox takeover / agent off)
    if getattr(cust, "agent_paused", False):
        return True

    # 3. already nudged enough since they last spoke
    done = await _followups_since_reply(db, cust)
    if done >= max_n:
        return True

    # 4. anything we sent recently — a nudge on top of the bot's own reply
    #    reads as pestering
    last_out = (
        await db.execute(
            select(func.max(Conversation.created_at)).where(
                Conversation.customer_id == cust.id,
                Conversation.direction == Direction.OUTBOUND,
            )
        )
    ).scalar_one()
    if last_out is not None and (now - last_out) < timedelta(hours=gap_h):
        return True
    return False


async def _followups_since_reply(db, cust: Customer) -> int:
    """How many nudges we have sent since the customer last wrote."""
    return (
        await db.execute(
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.customer_id == cust.id,
                Conversation.direction == Direction.OUTBOUND,
                Conversation.sent_by == SENT_BY,
                Conversation.created_at >= cust.last_message_at,
            )
        )
    ).scalar_one()


async def _history(db, cust: Customer) -> str:
    try:
        from app.services.knowledge import thread_history

        return await thread_history(db, customer_id=cust.id, limit=6)
    except Exception:
        log.exception("engage_history_failed", phone=cust.phone)
        return ""
