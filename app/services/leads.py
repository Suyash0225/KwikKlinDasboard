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
from app.services.tenant_context import manager_phone

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
        # source attribution: wa.me prefill codes (poster/google/bill/refer)
        first = (text or "").strip().split()[:2]
        code = " ".join(first).upper()
        source = "whatsapp"
        for tag, src in (
            ("POSTER", "poster"), ("GOOGLE", "google"),
            ("BILL", "bill"), ("REFER", "referral"), ("NAMASTE", "qr"),
        ):
            if tag in code:
                source = src
                break
        if lead is None:
            db.add(
                Lead(
                    phone=customer.phone, name=customer.name,
                    items_text=text[:300], stage="CONTACTED", source=source,
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


_HOT_WORDS = {  # intent keywords -> heat points
    "pickup": 3, "kab": 3, "aaj": 3, "kal": 3, "address": 2, "ghar": 2,
    "rate": 2, "price": 2, "kitna": 2, "charge": 2, "urgent": 3, "chahiye": 2,
}


async def run_hot_lead_digest() -> int:
    """09:00: owner gets the 5 hottest open leads — a human call converts."""
    from app.models import Conversation, Direction

    async with async_session_factory() as db:
        open_leads = (
            (
                await db.execute(
                    select(Lead).where(Lead.stage.in_(("CONTACTED", "INTERESTED")))
                )
            )
            .scalars()
            .all()
        )
        if not open_leads:
            return 0
        scored = []
        for lead in open_leads:
            cust = (
                await db.execute(select(Customer).where(Customer.phone == lead.phone))
            ).scalar_one_or_none()
            last_in = ""
            if cust:
                row = (
                    await db.execute(
                        select(Conversation.message_text)
                        .where(
                            Conversation.customer_id == cust.id,
                            Conversation.direction == Direction.INBOUND,
                        )
                        .order_by(Conversation.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                last_in = row or ""
            score = sum(
                pts for w, pts in _HOT_WORDS.items() if w in last_in.lower()
            ) + (3 if lead.stage == "INTERESTED" else 0)
            scored.append((score, lead, last_in))
        scored.sort(key=lambda t: t[0], reverse=True)
        lines = ["🔥 Aaj ke hot leads (call maar do, order pakka hota hai):"]
        for score, lead, last_in in scored[:5]:
            heat = "🔥🔥" if score >= 5 else ("🔥" if score >= 3 else "·")
            lines.append(
                f"{heat} {lead.name or lead.phone} ({lead.source}) — "
                f"\"{last_in[:50]}\""
            )
        try:
            await send_message(db, to_phone=manager_phone(), text="\n".join(lines))
        except SendError:
            log.info("hot_digest_not_sent")
        await audit.record(
            actor_role="system", actor="marketing", action="hot_lead_digest",
            args={"open": len(open_leads)}, result=f"top {min(5, len(scored))}",
        )
        return len(scored)


async def check_stop_throttle() -> None:
    """STOP badh rahe = messages zyada — cap khud 1 pe girao, owner ko batao."""
    from app.models import AuditLog
    from app.services import app_settings

    async with async_session_factory() as db:
        stops = (
            await db.execute(
                select(AuditLog.id).where(
                    AuditLog.action == "stop_optout",
                    AuditLog.at >= datetime.now(timezone.utc) - timedelta(days=7),
                )
            )
        ).scalars().all()
        cap = int(await app_settings.get(db, "marketing_freq_cap_per_month"))
        if len(stops) >= 3 and cap > 1:
            await app_settings.set_value(db, "marketing_freq_cap_per_month", 1)
            try:
                await send_message(
                    db, to_phone=manager_phone(),
                    text=(
                        f"⚠️ Hafte mein {len(stops)} logon ne STOP kiya — messages "
                        "zyada ja rahe the. Maine marketing limit khud 2 se 1 kar "
                        "di (Settings se wapas badha sakte ho)."
                    ),
                )
            except SendError:
                pass


async def check_marketing_eligible_bulk(
    db: AsyncSession, customer_ids: list
) -> dict:
    """Same gate as check_marketing_eligible, for a whole segment at once.

    The per-customer version costs ~6 queries; queueing a 500-person
    campaign was 3000 round trips before the first message went out. This
    is a fixed 5, whatever the reach. Reasons match the single-customer
    version exactly — one is not allowed to be laxer than the other.
    """
    from datetime import datetime as _dt

    from app.models import AuditLog, Escalation
    from app.services import app_settings

    ids = list(customer_ids)
    verdict: dict = {}
    if not ids:
        return verdict

    now = _dt.now(timezone.utc)
    month_ago = now - timedelta(days=30)

    cap = int(await app_settings.get(db, "marketing_freq_cap_per_month"))
    min_gap = timedelta(days=max(1, 30 // max(cap, 1)))

    customers = {
        c.id: c
        for c in (
            await db.execute(select(Customer).where(Customer.id.in_(ids)))
        ).scalars().all()
    }
    complained = set(
        (
            await db.execute(
                select(Escalation.customer_id)
                .where(
                    Escalation.customer_id.in_(ids),
                    Escalation.question.like("COMPLAINT:%"),
                    Escalation.created_at >= month_ago,
                )
                .group_by(Escalation.customer_id)
            )
        ).scalars().all()
    )
    busy = set(
        (
            await db.execute(
                select(Order.customer_id)
                .where(Order.customer_id.in_(ids), Order.status.in_(ACTIVE_STATUSES))
                .group_by(Order.customer_id)
            )
        ).scalars().all()
    )
    phones = {c.phone: cid for cid, c in customers.items() if c.phone}
    bad_rated = set()
    if phones:
        rated = (
            await db.execute(
                select(AuditLog.actor)
                .where(
                    AuditLog.action == "rating",
                    AuditLog.at >= month_ago,
                    AuditLog.actor.in_(tuple(phones)),
                    AuditLog.result.like("%agent paused%"),
                )
                .group_by(AuditLog.actor)
            )
        ).scalars().all()
        bad_rated = {phones[p] for p in rated if p in phones}

    # Order matters: it is the reason the owner sees on the skipped row.
    for cid in ids:
        cust = customers.get(cid)
        if cust is None or not cust.is_active:
            verdict[cid] = (False, "inactive")
        elif cust.opted_out or cust.marketing_opt_out:
            verdict[cid] = (False, "opted_out")
        elif cust.last_marketing_at and now - cust.last_marketing_at < min_gap:
            verdict[cid] = (False, "freq_cap")
        elif cid in complained:
            verdict[cid] = (False, "recent_complaint")
        elif cid in busy:
            verdict[cid] = (False, "active_order")
        elif cid in bad_rated:
            verdict[cid] = (False, "recent_bad_rating")
        else:
            verdict[cid] = (True, "")
    return verdict


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
