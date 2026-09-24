"""Human handoff: let the owner/staff answer from the real WhatsApp phone first.

Flow:
1. Phone sends an outbound message -> webhook records sent_by="human".
2. Customer sends a new inbound -> normal AI reply is held.
3. If no human outbound follows that inbound for the configured grace period,
   this worker lets the Service Agent take over.
"""

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models import Conversation, Customer, Direction
from app.services import app_settings
from app.services.whatsapp import SendError, send_message

log = structlog.get_logger()


async def _latest_inbound(db: AsyncSession, customer_id):
    return (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.customer_id == customer_id,
                Conversation.direction == Direction.INBOUND,
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_outbound(db: AsyncSession, customer_id):
    return (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.customer_id == customer_id,
                Conversation.direction == Direction.OUTBOUND,
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_human(db: AsyncSession, customer_id):
    return (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.customer_id == customer_id,
                Conversation.direction == Direction.OUTBOUND,
                Conversation.sent_by == "human",
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _reply_one(db: AsyncSession, customer: Customer, inbound: Conversation, grace_minutes: int) -> bool:
    """Return True if this customer got an AI takeover reply."""
    latest_human = await _latest_human(db, customer.id)
    latest_outbound = await _latest_outbound(db, customer.id)

    # A human reply must exist before this customer turn.
    if latest_human is None or latest_human.created_at >= inbound.created_at:
        return False

    # If anything automated/human was sent after the inbound, this turn is
    # already handled. This prevents duplicate AI replies.
    if latest_outbound is not None and latest_outbound.created_at > inbound.created_at:
        return False

    if datetime.now(timezone.utc) - inbound.created_at < timedelta(minutes=grace_minutes):
        return False

    # Import lazily to avoid webhook <-> service import cycles.
    from app.routers.webhook import _build_customer_reply, _customer_agent_enabled
    from app.services.ai_agent import build_ai_reply
    from app.services.scheduler import _claim, _unclaim

    key = f"human-handoff:{customer.id}:{inbound.wa_message_id or inbound.id}"
    if not await _claim(key):
        return False

    try:
        # Re-check after claiming in case a new customer/human message landed.
        current_inbound = await _latest_inbound(db, customer.id)
        current_outbound = await _latest_outbound(db, customer.id)
        if current_inbound is None or current_inbound.id != inbound.id:
            await _unclaim(key)
            return False
        if current_outbound is not None and current_outbound.created_at > inbound.created_at:
            await _unclaim(key)
            return False
        if customer.agent_paused or customer.opted_out:
            await _unclaim(key)
            return False
        if not await _customer_agent_enabled(db, customer):
            await _unclaim(key)
            return False

        text = current_inbound.message_text or ""
        reply = await build_ai_reply(db, customer, text)
        if reply is None:
            reply = await _build_customer_reply(db, customer, text)
        if not reply:
            await _unclaim(key)
            return False

        # Final race check immediately before the WhatsApp send.
        current_inbound = await _latest_inbound(db, customer.id)
        current_outbound = await _latest_outbound(db, customer.id)
        if current_inbound is None or current_inbound.id != inbound.id:
            await _unclaim(key)
            return False
        if current_outbound is not None and current_outbound.created_at > inbound.created_at:
            await _unclaim(key)
            return False

        try:
            await send_message(db, to_phone=customer.phone, text=reply, sent_by="ai")
        except SendError as exc:
            if not exc.transient:
                await _unclaim(key)
            log.exception("human_handoff_ai_send_failed", phone=customer.phone)
            return False

        log.info(
            "human_handoff_ai_resumed",
            phone=customer.phone,
            grace_minutes=grace_minutes,
        )
        return True
    except Exception:
        await _unclaim(key)
        log.exception("human_handoff_customer_failed", phone=customer.phone)
        return False


async def run_human_handoff() -> int:
    """Take over customer turns that have waited the configured grace period."""
    sent = 0
    async with async_session_factory() as db:
        grace_minutes = int(
            await app_settings.get(db, "human_handoff_grace_minutes") or 15
        )
        if grace_minutes <= 0:
            return 0

        customers = (
            await db.execute(
                select(Customer).where(
                    Customer.is_active.is_(True),
                    Customer.opted_out.is_(False),
                )
            )
        ).scalars().all()

        for customer in customers:
            inbound = await _latest_inbound(db, customer.id)
            if inbound is None:
                continue
            if await _reply_one(db, customer, inbound, grace_minutes):
                sent += 1
    return sent
