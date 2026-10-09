"""Marketing Agent: lead pipeline (owner's spec).

NEW -> CONTACTED -> INTERESTED -> CONVERTED / LOST (+ DORMANT for old
customers, handled by segments). Conversion ladder: ~2h, 1d, 3d, 7d.
A normal reply means "engaged" — it does NOT stop conversion follow-up;
explicit opt-out is handled separately by the webhook.
"""

from datetime import datetime, timedelta, timezone
import re

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
from app.services.team import primary_admin_phone

log = structlog.get_logger()


def _source_from_message(text: str | None) -> str:
    """Extract a bounded source label from a website WhatsApp prefill or legacy QR tag."""
    body = text or ""
    if re.search(r"(?im)^Lead source:\s*website\s*$", body):
        utm = re.search(r"(?im)^utm_source:\s*([^\r\n]{1,100})", body)
        if utm and utm.group(1).strip().lower() == "google":
            return "google"
        return "website"
    first = " ".join(body.strip().split()[:2]).upper()
    for tag, source in (("POSTER", "poster"), ("GOOGLE", "google"),
                        ("BILL", "bill"), ("REFER", "referral"), ("NAMASTE", "qr")):
        if tag in first:
            return source
    return "whatsapp"

def _utm_value(text: str | None, key: str) -> str | None:
    """Read a bounded UTM field from the website WhatsApp prefill."""
    body = text or ""
    if not re.search(r"(?im)^Lead source:\s*website\s*$", body):
        return None
    match = re.search(rf"(?im)^{re.escape(key)}:\s*([^\r\n]{{1,100}})", body)
    return match.group(1).strip()[:100] or None if match else None

_LADDER = [  # (followup_count -> delay after the previous customer touch, days, message key)
    (0, 2 / 24, "lead_followup_2h"),
    (1, 1, "lead_day1"),
    (2, 2, "lead_day3"),
    (3, 4, "lead_day7"),
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
        source = _source_from_message(text)
        if lead is None:
            db.add(
                Lead(
                    phone=customer.phone, name=customer.name,
                    items_text=text[:300], stage="CONTACTED", source=source,
                    source_medium=_utm_value(text, "utm_medium"),
                    source_campaign=_utm_value(text, "utm_campaign"),
                    last_contact_at=now, next_followup_at=now + timedelta(hours=2),
                )
            )
            await db.commit()
            await audit.record(
                actor_role="system", actor="marketing", action="lead_created",
                args={"phone": customer.phone, "text": text[:120]}, result="CONTACTED",
            )
            # New lead: notify the configured primary admin immediately.
            # Do not use the public/shop WhatsApp number as the owner alert target.
            try:
                await send_message(
                    db,
                    to_phone=await primary_admin_phone(db),
                    text=(
                        "*🔔 NEW LEAD*\n"
                        "━━━━━━━━━━━━━━\n"
                        f"*Name:* {customer.name or 'Unknown'}\n"
                        f"*Phone:* {customer.phone}\n"
                        f"*Message:* {text[:700]}\n"
                        "━━━━━━━━━━━━━━\n"
                        "AI has replied to the customer. Please follow up if needed."
                    ),
                    sent_by="bot",
                )
            except SendError:
                log.exception("new_lead_admin_alert_failed", phone=customer.phone)
        else:
            # A reply is a positive signal, not a reason to abandon the lead.
            # Keep the conversion ladder alive, but give the customer a
            # breathing window before the next proactive nudge.
            if lead.stage in ("CONTACTED", "LOST"):
                lead.stage = "INTERESTED"
            lead.items_text = (text or lead.items_text or "")[:300]
            lead.last_contact_at = now
            lead.next_followup_at = now + timedelta(hours=6)
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
            # Copy attribution only to the first conversion order. Repeat orders
            # must not overwrite the original acquisition source.
            order = (
                await db.execute(
                    select(Order)
                    .join(Customer, Customer.id == Order.customer_id)
                    .where(Customer.phone == phone)
                    .order_by(Order.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if order is not None and not order.acquisition_source:
                order.acquisition_source = lead.source or "unattributed"
                order.acquisition_campaign = lead.source_campaign
            lead.stage = "CONVERTED"
            lead.next_followup_at = None
            await db.commit()
    except Exception:
        log.exception("lead_convert_failed")


async def run_lead_followups(now: datetime | None = None) -> int:
    """Send due conversion nudges; only when marketing is explicitly auto.

    This is intentionally called hourly. The first nudge is ~2h after an
    enquiry, then 1d/3d/7d. Replies keep the lead in the funnel; creating an
    order converts it immediately via mark_converted().
    """
    # Lead follow-ups are proactive marketing. Never send them merely because
    # a lead exists: an unknown/new WhatsApp inquiry can become a Lead without
    # the owner explicitly opting into autonomous marketing. The owner must
    # select Marketing autonomy = auto before this scheduler may send.
    from app.services import app_settings

    async with async_session_factory() as settings_db:
        autonomy = str(await app_settings.get(settings_db, "marketing_autonomy")).lower()
    if autonomy != "auto":
        log.info("lead_followups_skipped_not_auto", autonomy=autonomy)
        return 0

    now = now or datetime.now(timezone.utc)
    # The scheduler ticks hourly, but proactive customer messages stay inside
    # the configured daytime window in India.
    now_ist = now.astimezone(timezone(timedelta(hours=5, minutes=30)))
    if now_ist.hour < 9 or now_ist.hour >= 21:
        return 0
    sends = 0
    async with async_session_factory() as db:
        due = (
            (
                await db.execute(
                    select(Lead).where(
                        Lead.stage.in_(("CONTACTED", "INTERESTED")),
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
                    db,
                    to_phone=lead.phone,
                    text=get_message(key, name=lead.name or "ji"),
                )
                sends += 1
            except SendError:
                log.info("lead_followup_not_sent", phone=lead.phone)
                # Do not consume a follow-up attempt when WhatsApp rejected it.
                lead.next_followup_at = now + timedelta(hours=1)
                await db.commit()
                continue

            lead.followup_count += 1
            lead.last_contact_at = now
            if lead.followup_count >= len(_LADDER):
                lead.stage = "LOST"
                lead.next_followup_at = now + timedelta(days=90)
            else:
                _, delay_days, _ = _LADDER[lead.followup_count]
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
