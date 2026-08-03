"""Scheduled jobs (APScheduler, Asia/Kolkata) — the proactive half of the agent.

Jobs:
- 10:00  daily staff standup: each active staff member gets their real
         pending list and is asked what's done/stuck.
- 11:00  delivery-day nudges: orders due TODAY and not Ready -> staff nudge;
         payment reminders (3 days polite / 15+ days firmer + admin flag).
- 21:30  nightly: recompute customer segments (marketing).
- Mon 10:30  weekly marketing suggestion to the admin.

Safety rails, enforced in code:
- Quiet hours (settings QUIET_HOURS): no proactive sends outside 09-21.
- Idempotency: every send claims a sent_events key first — a restarted
  server can never double-send the same event on the same day.
- Every job logs to audit_log; failures never kill the scheduler.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session_factory
from app.models import Customer, Order, PaymentStatus, SentEvent, Staff
from app.services import app_settings, audit
from app.services.messages import get_message, status_label
from app.services.order_service import ACTIVE_STATUSES
from app.services.whatsapp import SendError, WindowClosedError, send_message
from app.services.work_orders import items_summary

log = structlog.get_logger()

IST = ZoneInfo("Asia/Kolkata")

_scheduler: AsyncIOScheduler | None = None


def start() -> None:
    """Create and start all jobs. Called once from app startup."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone=IST)
    # Standup hour is read from settings at FIRE time inside the job would be
    # ideal, but cron needs a static hour — so we check inside and run the
    # trigger hourly, firing only when the configured hour matches.
    _scheduler.add_job(_hourly_tick, CronTrigger(minute=0, timezone=IST), id="hourly")
    _scheduler.add_job(
        _nightly_tick, CronTrigger(hour=21, minute=30, timezone=IST), id="nightly"
    )
    _scheduler.add_job(
        _tunnel_tick, CronTrigger(minute="*/10", timezone=IST), id="tunnel-guard"
    )
    _scheduler.start()
    log.info("scheduler_started", jobs=["hourly", "nightly"])


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def _in_quiet_hours(now_ist: datetime) -> bool:
    start_h, end_h = settings.QUIET_HOURS_START, settings.QUIET_HOURS_END
    h = now_ist.hour
    if start_h > end_h:  # e.g. 21 -> 9, wraps midnight
        return h >= start_h or h < end_h
    return start_h <= h < end_h


async def _claim(event_key: str) -> bool:
    """Atomically claim an idempotency key. False = already sent."""
    async with async_session_factory() as s:
        s.add(SentEvent(event_key=event_key[:120]))
        try:
            await s.commit()
            return True
        except IntegrityError:
            await s.rollback()
            return False


async def _hourly_tick() -> None:
    """One entry point, so a bad hour never skips the others silently."""
    now_ist = datetime.now(IST)
    try:
        async with async_session_factory() as db:
            standup_hour = int(await app_settings.get(db, "standup_hour"))
        if now_ist.hour == standup_hour:
            await run_standup()
    except Exception:
        log.exception("standup_job_failed")
    try:
        if now_ist.hour == 11:
            await run_delivery_nudges()
            await run_payment_reminders()
    except Exception:
        log.exception("reminder_jobs_failed")
    # daily social poster (Instagram + owner's GMB pack)
    try:
        async with async_session_factory() as db:
            social_hour = int(await app_settings.get(db, "social_post_hour"))
        if now_ist.hour == social_hour:
            from app.services.social import run_daily_social

            await run_daily_social()
    except Exception:
        log.exception("daily_social_failed")
    # resume campaigns that paused for quiet hours / restarts
    try:
        if not _in_quiet_hours(now_ist):
            import asyncio

            from app.models import Campaign
            from app.services.marketing import send_campaign

            async with async_session_factory() as db:
                stuck = (
                    (
                        await db.execute(
                            select(Campaign).where(
                                Campaign.status.in_(("approved", "sending"))
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            for c in stuck:
                asyncio.create_task(send_campaign(c.id))
    except Exception:
        log.exception("campaign_resume_failed")


async def _tunnel_tick() -> None:
    try:
        from app.services.tunnel_guard import check_and_heal

        await check_and_heal()
    except Exception:
        log.exception("tunnel_guard_failed")


async def _nightly_tick() -> None:
    try:
        from app.services.marketing import recompute_segments

        async with async_session_factory() as db:
            await recompute_segments(db)
    except Exception:
        log.exception("nightly_segments_failed")
    try:
        now_ist = datetime.now(IST)
        if now_ist.weekday() == 0:  # Monday night: weekly suggestion
            from app.services.marketing_agent import weekly_suggestion

            await weekly_suggestion()
    except Exception:
        log.exception("weekly_suggestion_failed")


async def run_standup(force: bool = False) -> int:
    """Message each active staff member their pending list. Returns sends."""
    now_ist = datetime.now(IST)
    if not force and _in_quiet_hours(now_ist):
        log.info("standup_skipped_quiet_hours")
        return 0
    today_key = now_ist.strftime("%Y-%m-%d")
    sends = 0
    async with async_session_factory() as db:
        staff_rows = (
            (await db.execute(select(Staff).where(Staff.is_active))).scalars().all()
        )
        default_phone = await app_settings.get(db, "default_washer_phone")
        for st in staff_rows:
            orders = await _pending_orders_for(db, st, default_phone)
            if not orders:
                continue
            if not await _claim(f"standup:{today_key}:{st.phone}"):
                continue
            lines = [get_message("standup_header", name=st.name, count=str(len(orders)))]
            for i, o in enumerate(orders[:10], 1):
                cust = await db.get(Customer, o.customer_id)
                flags = []
                if o.priority == "urgent":
                    flags.append("🔴 URGENT")
                if o.expected_delivery and o.expected_delivery <= date.today():
                    flags.append("aaj delivery")
                lines.append(
                    f"{i}. {o.order_number} — {cust.name or cust.phone if cust else '?'} — "
                    f"{items_summary(o)} — {status_label(o.status)}"
                    + (f" [{', '.join(flags)}]" if flags else "")
                )
            lines.append(get_message("standup_footer"))
            text = "\n".join(lines)
            try:
                await send_message(db, to_phone=st.phone, text=text)
                sends += 1
            except WindowClosedError:
                try:
                    await send_message(
                        db, to_phone=st.phone,
                        template_name="kk_staff_alert",
                        template_params=[" ".join(text.split())[:600]],
                    )
                    sends += 1
                except SendError:
                    log.warning("standup_not_sent", staff=st.name)
            except SendError:
                log.warning("standup_send_failed", staff=st.name)
        await audit.record(
            actor_role="system", actor="scheduler", action="standup",
            args={"date": today_key}, result=f"sent to {sends} staff",
        )
    return sends


async def _pending_orders_for(db, st: Staff, default_phone: str) -> list[Order]:
    """Orders this staff member is responsible for (assigned or default)."""
    cond = Order.status.in_(ACTIVE_STATUSES)
    q = select(Order).where(cond).order_by(Order.priority.desc(), Order.created_at)
    rows = (await db.execute(q)).scalars().all()
    mine = []
    is_default = bool(default_phone) and st.phone == default_phone
    for o in rows:
        if o.assigned_washer_id == st.id or o.assigned_delivery_id == st.id:
            mine.append(o)
        elif o.assigned_washer_id is None and is_default and st.role.name != "DELIVERY":
            mine.append(o)
    return mine


async def run_delivery_nudges() -> int:
    """Orders due today & not Ready/OFD/Delivered -> nudge responsible staff."""
    now_ist = datetime.now(IST)
    if _in_quiet_hours(now_ist):
        return 0
    from app.models import OrderStatus
    from app.services.work_orders import send_work_order

    today_key = now_ist.strftime("%Y-%m-%d")
    sends = 0
    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(Order).where(
                        Order.expected_delivery <= date.today(),
                        Order.status.in_(
                            (
                                OrderStatus.RECEIVED, OrderStatus.IN_WASH,
                                OrderStatus.IN_DRY, OrderStatus.IN_IRON,
                            )
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for o in rows:
            if not await _claim(f"duenudge:{today_key}:{o.order_number}"):
                continue
            outcome = await send_work_order(
                db, o, headline="⏰ Aaj delivery hai, abhi ready nahi",
                extra="Pehle ise nipta dein.",
            )
            if outcome in ("sent", "sent_template"):
                sends += 1
        if rows:
            await audit.record(
                actor_role="system", actor="scheduler", action="delivery_nudges",
                args={"orders": len(rows)}, result=f"nudged {sends}",
            )
    return sends


async def run_payment_reminders() -> int:
    """Polite at 3 days due, firmer at 15+ (owner's FOLLOWUP_DAYS) + admin flag."""
    now_ist = datetime.now(IST)
    if _in_quiet_hours(now_ist):
        return 0
    sends = 0
    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(Order, Customer)
                    .join(Customer, Customer.id == Order.customer_id)
                    .where(
                        Order.payment_status != PaymentStatus.PAID,
                        Order.total_amount.isnot(None),
                        Order.actual_delivery.isnot(None),
                    )
                )
            )
            .all()
        )
        firm_days = settings.FOLLOWUP_DAYS
        flagged: list[str] = []
        for o, cust in rows:
            if cust.opted_out or not cust.is_active:
                continue
            due = (o.total_amount or Decimal("0")) - (o.amount_paid or Decimal("0"))
            if due <= 0:
                continue
            age_days = (datetime.now(timezone.utc) - o.actual_delivery).days
            if age_days >= firm_days:
                key, kind = f"payrem-firm:{o.order_number}", "payment_reminder_firm"
                flagged.append(f"{o.order_number} ({cust.name or cust.phone}) ₹{due}")
            elif age_days >= 3:
                key, kind = f"payrem-3:{o.order_number}", "payment_reminder"
            else:
                continue
            if not await _claim(key):
                continue
            try:
                await send_message(
                    db, to_phone=cust.phone,
                    text=get_message(
                        kind, order_number=o.order_number, amount=f"{due}",
                    ),
                )
                sends += 1
            except SendError:
                log.info("payment_reminder_not_sent", order=o.order_number)
        if flagged and await _claim(f"payrem-adminflag:{now_ist.strftime('%Y-%m-%d')}"):
            try:
                await send_message(
                    db, to_phone=settings.MANAGER_PHONE,
                    text=get_message("overdue_admin_flag", listing="\n".join(flagged[:15])),
                )
            except SendError:
                log.info("overdue_admin_flag_not_sent")
        await audit.record(
            actor_role="system", actor="scheduler", action="payment_reminders",
            args={}, result=f"sent {sends}, flagged {len(flagged)}",
        )
    return sends
