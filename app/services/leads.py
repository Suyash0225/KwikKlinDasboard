"""Marketing Agent: lead pipeline (owner's spec).

NEW -> CONTACTED -> INTERESTED -> CONVERTED / LOST (+ DORMANT for old
customers, handled by segments). Ladder: day 0 (15 min!), 1, 3 (offer),
7 (last) -> LOST + 90 din silence. Any reply stops the ladder.
"""

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models import Customer, Lead, Order
from app.services import audit
from app.services.messages import get_message
from app.services.order_service import ACTIVE_STATUSES
from app.services.whatsapp import SendError, send_message

log = structlog.get_logger()

_LADDER = [  # (followup_count -> next delay days, message key)
    (0, 1, "lead_day1"),
    (1, 2, "lead_day3"),
    (2, 4, "lead_day7"),
]


async def note_inquiry(db: AsyncSession, customer: Customer, text: str) -> None:
    """Unknown/new number wrote in and has NO orders -> track as a lead.

    Called from the webhook after the AI reply (which IS the day-0
    15-minute response). Never raises.
    """
    try:
        has_order = (
            await db.execute(
                select(Order.id).where(Order.customer_id == customer.id).limit(1)
            )
        ).scalar_one_or_none()
        if has_order is not None:
            return
        lead = (
            await db.execute(select(Lead).where(Lead.phone == customer.phone))
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if lead is None:
            db.add(
                Lead(
                    phone=customer.phone, name=customer.name,
                    items_text=text[:300], stage="CONTACTED",
                    last_contact_at=now, next_followup_at=now + timedelta(days=1),
                )
            )
            await db.commit()
            await audit.record(
                actor_role="system", actor="marketing", action="lead_created",
                args={"phone": customer.phone, "text": text[:120]}, result="CONTACTED",
            )
        elif lead.stage in ("CONTACTED", "LOST"):
            # they replied — ladder stops, humans/AI talk normally
            lead.stage = "INTERESTED"
            lead.next_followup_at = None
            lead.last_contact_at = now
            await db.commit()
    except Exception:
        log.exception("lead_note_failed")


async def mark_converted(db: AsyncSession, phone: str) -> None:
    """First order created -> lead becomes a customer. Never raises."""
    try:
        lead = (
            await db.execute(select(Lead).where(Lead.phone == phone))
        ).scalar_one_or_none()
        if lead and lead.stage != "CONVERTED":
            lead.stage = "CONVERTED"
            lead.next_followup_at = None
            await db.commit()
    except Exception:
        log.exception("lead_convert_failed")


async def run_lead_followups() -> int:
    """Daily: send due ladder messages; after step 3 -> LOST."""
    now = datetime.now(timezone.utc)
    sends = 0
    async with async_session_factory() as db:
        due = (
            (
                await db.execute(
                    select(Lead).where(
                        Lead.stage == "CONTACTED",
                        Lead.next_followup_at.isnot(None),
                        Lead.next_followup_at <= now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for lead in due:
            step = next(
                (s for s in _LADDER if s[0] == lead.followup_count), None
            )
            if step is None:
                lead.stage = "LOST"
                lead.next_followup_at = now + timedelta(days=90)
                await db.commit()
                continue
            _, delay_days, key = step
            try:
                await send_message(
                    db, to_phone=lead.phone,
                    text=get_message(key, name=lead.name or "ji"),
                )
                sends += 1
            except SendError:
                log.info("lead_followup_not_sent", phone=lead.phone)
            lead.followup_count += 1
            lead.last_contact_at = now
            if lead.followup_count > len(_LADDER):
                lead.stage = "LOST"
                lead.next_followup_at = now + timedelta(days=90)
            else:
                lead.next_followup_at = now + timedelta(days=delay_days)
            await db.commit()
        if due:
            await audit.record(
                actor_role="system", actor="marketing", action="lead_followups",
                args={"due": len(due)}, result=f"sent {sends}",
            )
    return sends


async def check_marketing_eligible(db: AsyncSession, customer_id) -> tuple[bool, str]:
    """THE single gate (owner's spec): opt-out, active order, monthly cap,
    recent bad rating — one call, so nothing is ever forgotten."""
    from app.services.marketing import eligible  # opt-out + cap + complaints

    ok, reason = await eligible(db, customer_id)
    if not ok:
        return ok, reason
    active = (
        await db.execute(
            select(Order.id).where(
                Order.customer_id == customer_id,
                Order.status.in_(ACTIVE_STATUSES),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if active is not None:
        return False, "active_order"
    from app.models import AuditLog

    bad = (
        await db.execute(
            select(AuditLog.id).where(
                AuditLog.action == "rating",
                AuditLog.at >= datetime.now(timezone.utc) - timedelta(days=30),
                AuditLog.actor
                == (await db.get(Customer, customer_id)).phone,
                AuditLog.result.like("%agent paused%"),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if bad is not None:
        return False, "recent_bad_rating"
    return True, ""
