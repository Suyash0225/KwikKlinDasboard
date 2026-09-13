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
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session_factory
from app.models import Customer, Order, OrderStatus, PaymentStatus, SentEvent, Staff
from app.services import app_settings, audit
from app.services.messages import get_message, status_label
from app.services.order_service import ACTIVE_STATUSES
from app.services.whatsapp import SendError, WindowClosedError, send_message
from app.services.work_orders import items_summary
from app.services.tenant_context import manager_phone

log = structlog.get_logger()

IST = ZoneInfo("Asia/Kolkata")

_scheduler: AsyncIOScheduler | None = None


def start() -> None:
    """Create and start all jobs. Called once from app startup."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(
        timezone=IST,
        job_defaults={
            # A tick delayed by event-loop load (up to 5 min) still fires
            # instead of being dropped; queued-up misfires collapse into one.
            "coalesce": True,
            "misfire_grace_time": 300,
            "max_instances": 1,
        },
    )
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
    # Durability loop: replay stuck webhook events + drain the outbound
    # retry queue. This is what makes "order kabhi lost nahi hota" true.
    _scheduler.add_job(
        _durability_tick, CronTrigger(minute="*/5", timezone=IST), id="durability"
    )
    _scheduler.start()
    log.info("scheduler_started", jobs=["hourly", "nightly", "tunnel-guard", "durability"])


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


def _key(event_key: str) -> str:
    """sent_events ki key global hai — tenant ka prefix lagao, warna dukaan
    A ka 'daysum:2026-09-12' dukaan B ka summary rok deta."""
    from app.services.tenant_context import claim_prefix

    return (claim_prefix() + event_key)[:120]


async def _claim(event_key: str) -> bool:
    """Atomically claim an idempotency key. False = already sent."""
    async with async_session_factory() as s:
        s.add(SentEvent(event_key=_key(event_key)))
        try:
            await s.commit()
            return True
        except IntegrityError:
            await s.rollback()
            return False


async def _unclaim(event_key: str) -> None:
    """Release a claimed key after a permanent send failure, so a later tick
    can try again instead of the notification being lost forever."""
    from sqlalchemy import delete

    try:
        async with async_session_factory() as s:
            await s.execute(delete(SentEvent).where(SentEvent.event_key == _key(event_key)))
            await s.commit()
    except Exception:
        log.exception("unclaim_failed", event_key=event_key)


async def _durability_tick() -> None:
    """Every 5 min: replay failed/stuck webhook events, drain outbound queue."""
    try:
        from app.routers.webhook import retry_stuck_webhook_events

        n = await retry_stuck_webhook_events()
        if n:
            log.info("webhook_events_retried", count=n)
    except Exception:
        log.exception("webhook_retry_job_failed")
    try:
        from app.services.whatsapp import drain_outbound_queue

        n = await drain_outbound_queue()
        if n:
            log.info("outbound_queue_drained", count=n)
    except Exception:
        log.exception("outbox_drain_failed")


async def _for_each_tenant(job_name: str, fn) -> None:
    """Har chalu dukaan ke liye fn(now_ist) alag context mein.

    Pehle ye jobs bina tenant ke chalti thin — RLS band, ORM filter band —
    to daily summary SAB dukaanon ke order gin kar .env wale malik ko jaata
    tha, aur payment reminder doosri dukaan ke grahak ko is dukaan ke
    number se. Ab har dukaan apne context mein: apna data, apna WhatsApp
    number, apna malik. Ek dukaan ka crash doosri ko nahi rokta."""
    from app.services.tenant_context import active_tenants, as_tenant

    try:
        tenants = await active_tenants()
    except Exception:
        log.exception("tenant_list_failed", job=job_name)
        return
    now_ist = datetime.now(IST)
    for tid, slug, owner_phone in tenants:
        try:
            async with as_tenant(tid, owner_phone or None):
                await fn(now_ist)
        except Exception:
            log.exception("tenant_job_failed", job=job_name, tenant=slug)


async def _hourly_tick() -> None:
    """One entry point, so a bad hour never skips the others silently."""
    await _for_each_tenant("hourly", _hourly_for_tenant)
    await _hourly_platform(datetime.now(IST))


async def _hourly_for_tenant(now_ist: datetime) -> None:
    """Ek dukaan ka ghante ka kaam — context set hai, sab scoped hai."""
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
    # 09:00: hot-lead digest to the owner (human call = best converter)
    try:
        if now_ist.hour == 9 and await _claim(f"hotdigest:{now_ist.strftime('%Y-%m-%d')}"):
            from app.services.leads import run_hot_lead_digest

            await run_hot_lead_digest()
    except Exception:
        log.exception("hot_digest_failed")
    # 10:00 daily: lead follow-up ladder; 1st of month: marketing report
    try:
        if now_ist.hour == 10:
            from app.services.leads import run_lead_followups

            await run_lead_followups()
        if now_ist.day == 1 and now_ist.hour == 10 and await _claim(
            f"mktreport:{now_ist.strftime('%Y-%m')}"
        ):
            from app.models import Lead

            async with async_session_factory() as db:
                rows = (
                    await db.execute(select(Lead.stage, func.count()).group_by(Lead.stage))
                ).all()
                stages = " | ".join(f"{s}: {c}" for s, c in rows) or "koi lead nahi"
                stops = (
                    await db.execute(
                        select(func.count()).select_from(Customer).where(Customer.opted_out)
                    )
                ).scalar_one()
                try:
                    await send_message(
                        db, to_phone=manager_phone(),
                        text=f"📊 Mahine ki marketing report:\nLeads: {stages}\nSTOP kiye hue: {stops}",
                    )
                except SendError as exc:
                    if not exc.transient:  # transient goes via outbound_queue
                        await _unclaim(f"mktreport:{now_ist.strftime('%Y-%m')}")
    except Exception:
        log.exception("lead_jobs_failed")
    # every 2 hours 08-20: stale-order follow-up pings (owner's spec)
    try:
        if 8 <= now_ist.hour <= 20 and now_ist.hour % 2 == 0:
            await run_follow_up_pings()
    except Exception:
        log.exception("follow_up_pings_failed")
    # assigned tasks: chase whoever owes an answer, hourly (the service
    # itself decides who is actually due, and respects quiet hours)
    try:
        from app.services.tasks import run_task_followups

        await run_task_followups()
    except Exception:
        log.exception("task_followups_failed")
    # live customer conversations that went quiet: one warm, useful nudge
    # while their window is open (engage.py decides who is actually due)
    try:
        from app.services.engage import run_conversation_followups

        await run_conversation_followups()
    except Exception:
        log.exception("conversation_followups_failed")
    # 18:00 evening washer status round; 21:00 owner summary
    try:
        if now_ist.hour == 18:
            await run_standup(force=True, key_prefix="standup-eve")
    except Exception:
        log.exception("evening_standup_failed")
    try:
        if now_ist.hour == 21:
            await run_daily_summary()
    except Exception:
        log.exception("daily_summary_failed")
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


async def _hourly_platform(now_ist: datetime) -> None:
    """Jo kaam dukaan ka nahi, platform ka hai — .env wale (home) number ka
    Meta block watcher. Home tenant ke context mein, ek baar."""
    from app.services.tenant_context import as_tenant, get_home_tenant_id

    home = await get_home_tenant_id()
    if home is None:
        return
    async with as_tenant(home):
        await _meta_block_watch(now_ist)


async def _meta_block_watch(now_ist: datetime) -> None:
    # Meta block watcher: probe hourly; the moment access returns, tell
    # the owner (the send itself only works once unblocked — perfect signal)
    try:
        import httpx as _hx

        async with _hx.AsyncClient(timeout=15) as _c:
            _r = await _c.get(
                f"https://graph.facebook.com/v21.0/{settings.WHATSAPP_PHONE_NUMBER_ID}",
                headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
                params={"fields": "display_phone_number"},
            )
        if _r.status_code == 200 and await _claim(
            f"meta-unblocked:{now_ist.strftime('%Y-%m-%d-%H')}"
        ):
            # block ke दौरान tunnel badla ho sakta hai — webhook turant sync
            from app.services import app_settings as _as
            from app.services.tunnel_guard import _update_meta_webhook

            async with async_session_factory() as db:
                base = (await _as.get(db, "public_base_url") or "").rstrip("/")
            hooked = await _update_meta_webhook(base) if base else False
            async with async_session_factory() as db:
                try:
                    await send_message(
                        db, to_phone=manager_phone(),
                        text="🎉 Meta ka block hat gaya! Bot wapas zinda hai"
                             + (" — webhook bhi sync ✅" if hooked else " (webhook sync retry hoga)")
                             + ". Kuch karna nahi hai.",
                    )
                except SendError:
                    pass
    except Exception:
        pass  # probe must never disturb the tick


async def _tunnel_tick() -> None:
    try:
        from app.services.tunnel_guard import check_and_heal

        await check_and_heal()
    except Exception:
        log.exception("tunnel_guard_failed")


# Owner's Order Agent spec: stale thresholds (hours) per status, and who
# to ping. 6h stale -> manager escalation.
_PING_RULES = {
    OrderStatus.PICKUP_ASSIGNED: (2, "DELIVERY", "Pickup hua ya nahi?"),
    OrderStatus.PICKED_UP: (3, "WASHER", "Lot mila? Washing shuru?"),
    OrderStatus.READY: (2, "DELIVERY", "Packed hai — delivery pe kab nikal rahe ho?"),
    OrderStatus.OUT_FOR_DELIVERY: (2, "DELIVERY", "Deliver hua ya nahi?"),
}


async def run_follow_up_pings() -> int:
    """Every 2h (08-21 IST): ping the responsible person on stale orders;
    6h+ stale -> manager escalation. Idempotent per 2h window."""
    from app.models import OrderStatusHistory
    from app.services.work_orders import send_work_order

    from app.models import TASK_OPEN, Task
    from app.services.tasks import JOB_KINDS

    now = datetime.now(timezone.utc)
    now_ist = datetime.now(IST)
    if _in_quiet_hours(now_ist):
        return 0
    window_key = now_ist.strftime("%Y-%m-%d-%H")
    sends = 0
    async with async_session_factory() as db:
        for status, (stale_h, role, question) in _PING_RULES.items():
            rows = (
                (await db.execute(select(Order).where(Order.status == status)))
                .scalars()
                .all()
            )
            for o in rows:
                last_change = (
                    await db.execute(
                        select(func.max(OrderStatusHistory.changed_at)).where(
                            OrderStatusHistory.order_id == o.id
                        )
                    )
                ).scalar_one() or o.created_at
                stale_hours = (now - last_change).total_seconds() / 3600
                if stale_hours < stale_h:
                    continue
                if stale_hours >= 6:
                    if await _claim(f"esc6h:{o.order_number}:{now_ist.strftime('%Y-%m-%d')}"):
                        try:
                            await send_message(
                                db, to_phone=manager_phone(),
                                text=(
                                    f"🚨 ESCALATION — {o.order_number}\n"
                                    f"Status: {o.status.name}, {int(stale_hours)} ghante se atka hai.\n"
                                    f"Staff jawab nahi de raha — khud dekh lein."
                                ),
                            )
                        except SendError as exc:
                            log.info("esc6h_not_sent")
                            if not exc.transient:
                                await _unclaim(
                                    f"esc6h:{o.order_number}:{now_ist.strftime('%Y-%m-%d')}"
                                )
                # A live pickup/delivery task already has its own follow-up
                # clock — two reminders for one job reads as spam.
                job_open = (
                    await db.execute(
                        select(Task.id).where(
                            Task.order_id == o.id,
                            Task.kind.in_(JOB_KINDS),
                            Task.status == TASK_OPEN,
                        )
                    )
                ).first()
                if job_open is not None:
                    continue
                if not await _claim(f"ping:{o.order_number}:{window_key}"):
                    continue
                outcome = await send_work_order(
                    db, o, headline=f"⏰ Reminder: {question}",
                    extra="Ho gaya to reply karein: done " + o.order_number,
                    role=role,
                )
                if outcome in ("sent", "sent_template"):
                    sends += 1
        if sends:
            await audit.record(
                actor_role="system", actor="scheduler", action="follow_up_pings",
                args={"window": window_key}, result=f"pinged {sends}",
            )
    return sends


async def run_daily_summary() -> None:
    """21:00 IST: owner's one-look day summary (internal — quiet-hour exempt)."""
    now_ist = datetime.now(IST)
    day_key = now_ist.strftime("%Y-%m-%d")
    if not await _claim(f"daysum:{day_key}"):
        return
    today_start = now_ist.replace(hour=0, minute=0, second=0).astimezone(timezone.utc)
    async with async_session_factory() as db:
        new_n = (
            await db.execute(
                select(func.count()).select_from(Order).where(Order.created_at >= today_start)
            )
        ).scalar_one()
        delivered_n = (
            await db.execute(
                select(func.count()).select_from(Order).where(
                    Order.actual_delivery >= today_start
                )
            )
        ).scalar_one()
        by_status = (
            await db.execute(
                select(Order.status, func.count())
                .where(Order.status.in_(ACTIVE_STATUSES))
                .group_by(Order.status)
            )
        ).all()
        late = (
            (
                await db.execute(
                    select(Order).where(
                        Order.expected_delivery < date.today(),
                        Order.status.notin_(
                            (OrderStatus.DELIVERED, OrderStatus.CANCELLED)
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        status_line = " | ".join(f"{s.name}: {c}" for s, c in by_status) or "koi active nahi"
        late_line = "\n".join(f"- {o.order_number} (tha {o.expected_delivery})" for o in late[:10]) or "koi nahi 🎉"
        text = (
            f"🌙 Aaj ka summary ({now_ist.strftime('%d %b')}):\n"
            f"Naye order: {new_n} | Deliver hue: {delivered_n}\n"
            f"Active: {status_line}\n"
            f"LATE ORDERS:\n{late_line}"
        )
        try:
            await send_message(db, to_phone=manager_phone(), text=text)
        except SendError as exc:
            log.info("daily_summary_not_sent")
            if not exc.transient:  # transient goes via outbound_queue
                await _unclaim(f"daysum:{day_key}")
        await audit.record(
            actor_role="system", actor="scheduler", action="daily_summary",
            args={"date": day_key}, result=f"new={new_n} late={len(late)}",
        )


async def _nightly_tick() -> None:
    # Postgres backup FIRST — everything else can fail, this must run.
    try:
        from app.services.backup import run_backup

        await run_backup()
    except Exception:
        log.exception("nightly_backup_failed")
    # Control-panel KPI snapshot (clients, MRR, past_due) — platform ka,
    # dukaan ka nahi; isliye jaan-boojh kar system context mein.
    try:
        from app.services.kpis import snapshot_today

        async with async_session_factory() as db:
            await snapshot_today(db)
    except Exception:
        log.exception("kpi_snapshot_failed")
    # Recharge credits ki validity khatam -> balance se kaato (ledger mein
    # trail rehta hai). Data kabhi delete nahi hota.
    try:
        from app.services.credits import expire_due_credits

        async with async_session_factory() as db:
            cut = await expire_due_credits(db)
        if cut["ai"] or cut["wa"]:
            log.info("credits_expiry_sweep", **cut)
    except Exception:
        log.exception("credits_expiry_failed")
    # Trial/subscription sweep: expired trial -> past_due (read-only),
    # 30-din grace khatam -> locked. Data kabhi delete nahi hota.
    try:
        from app.services.billing import run_subscription_sweep

        async with async_session_factory() as db:
            moved = await run_subscription_sweep(db)
        if moved["past_due"] or moved["locked"]:
            log.info("subscription_sweep", **moved)
    except Exception:
        log.exception("subscription_sweep_failed")
    # prune grow-forever tables (keys older than any retry window)
    try:
        from sqlalchemy import delete

        from app.models import OutboundMessage, WebhookEvent

        cutoff60 = datetime.now(timezone.utc) - timedelta(days=60)
        cutoff30 = datetime.now(timezone.utc) - timedelta(days=30)
        async with async_session_factory() as db:
            await db.execute(delete(SentEvent).where(SentEvent.at < cutoff60))
            await db.execute(
                delete(WebhookEvent).where(
                    WebhookEvent.status == "processed",
                    WebhookEvent.received_at < cutoff30,
                )
            )
            await db.execute(
                delete(OutboundMessage).where(
                    OutboundMessage.status == "sent",
                    OutboundMessage.created_at < cutoff30,
                )
            )
            await db.commit()
    except Exception:
        log.exception("nightly_prune_failed")
    # Dukaan-wise raat ka kaam: STOP-rate throttle, customer segments,
    # Monday ko weekly marketing suggestion (plan ke hisaab se).
    await _for_each_tenant("nightly", _nightly_for_tenant)


async def _nightly_for_tenant(now_ist: datetime) -> None:
    # STOP-rate auto-throttle (senior-architect P0)
    try:
        from app.services.leads import check_stop_throttle

        await check_stop_throttle()
    except Exception:
        log.exception("stop_throttle_failed")
    try:
        from app.services.marketing import recompute_segments

        async with async_session_factory() as db:
            await recompute_segments(db)
    except Exception:
        log.exception("nightly_segments_failed")
    try:
        if now_ist.weekday() == 0:  # Monday night: weekly suggestion
            # marketing_agent feature plan par depend karta hai — IS dukaan ka
            # plan, home ka nahi.
            from app.models.tenant import Tenant
            from app.services import plans as _plans
            from app.services.tenant_context import current_tenant_id

            async with async_session_factory() as db:
                me = await db.get(Tenant, current_tenant_id.get())
            if me is not None and not _plans.feature_on(me.plan, "marketing_agent"):
                log.info("marketing_agent_feature_off", plan=me.plan)
            else:
                from app.services.marketing_agent import weekly_suggestion

                await weekly_suggestion()
    except Exception:
        log.exception("weekly_suggestion_failed")


async def run_standup(force: bool = False, key_prefix: str = "standup") -> int:
    """Message each active staff member their pending list. Returns sends."""
    now_ist = datetime.now(IST)
    if not force and _in_quiet_hours(now_ist):
        log.info("standup_skipped_quiet_hours")
        return 0
    today_key = now_ist.strftime("%Y-%m-%d")
    sends = 0
    async with async_session_factory() as db:
        # Workers only — the owner gets the day summary, not a work list.
        from app.models import StaffRole

        staff_rows = (
            (
                await db.execute(
                    select(Staff).where(Staff.is_active, Staff.role != StaffRole.ADMIN)
                )
            )
            .scalars()
            .all()
        )
        default_phone = await app_settings.get(db, "default_washer_phone")
        for st in staff_rows:
            orders = await _pending_orders_for(db, st, default_phone)
            if not orders:
                continue
            if not await _claim(f"{key_prefix}:{today_key}:{st.phone}"):
                continue
            lines = [
                get_message(
                    "standup_header", greet=_greeting(now_ist),
                    name=st.name, count=str(len(orders)),
                )
            ]
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
                # Roz subah ki list bhi tappable — jis order par update dena
                # ho use chun lo, phir teen button. Ye wahi list hai jo staff
                # ke "order details do" poochhne par jaati hai.
                from app.services.work_orders import list_button_label, work_rows

                rows = await work_rows(db, orders[:10])
                await send_message(
                    db, to_phone=st.phone, text=text, list_rows=rows,
                    list_button=await list_button_label(db),
                    list_title="Aaj ka kaam",
                )
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


# Kis role ka kaam kis stage par hai. Delivery wale ko "kapde mil gaye,
# dhulai shuru hogi" wala order bhejna bekaar hai — uske paas tab kaam
# aata hai jab pickup karna ho ya kapde nikalne ho.
_WASH_STAGES = (
    OrderStatus.RECEIVED, OrderStatus.PICKED_UP, OrderStatus.IN_WASH,
    OrderStatus.IN_DRY, OrderStatus.IN_IRON,
)
_DELIVERY_STAGES = (
    OrderStatus.PICKUP_ASSIGNED, OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY,
)


def _greeting(now_ist: datetime) -> str:
    """Waqt ke hisaab se namaskaar. Standup ka waqt owner badal sakta hai
    (standup_hour), aur owner khud bhi kabhi bhi bhej sakta hai — isliye
    "Good morning" gaad dena galat tha."""
    h = now_ist.hour
    if h < 12:
        return "🌅 Good morning"
    if h < 17:
        return "☀️ Namaste"
    return "🌇 Good evening"


async def _pending_orders_for(db, st: Staff, default_phone: str) -> list[Order]:
    """Is aadmi ka aaj ka asli kaam — role ke hisaab se.

    Pehle yahan sirf "kya ye order iske naam par hai" dekha jaata tha.
    Isliye Ajit (delivery) ko dhulai wale order ki list chali gayi thi,
    jiska uske liye koi matlab hi nahi tha — aur asli delivery ka kaam
    usi bheed mein dab gaya.
    """
    cond = Order.status.in_(ACTIVE_STATUSES)
    q = select(Order).where(cond).order_by(Order.priority.desc(), Order.created_at)
    rows = (await db.execute(q)).scalars().all()
    is_delivery = st.role.name == "DELIVERY"
    stages = _DELIVERY_STAGES if is_delivery else _WASH_STAGES
    is_default = bool(default_phone) and st.phone == default_phone
    mine = []
    for o in rows:
        if o.status not in stages:
            continue
        mine_by_name = (
            o.assigned_delivery_id == st.id if is_delivery else o.assigned_washer_id == st.id
        )
        if mine_by_name:
            mine.append(o)
        elif o.assigned_washer_id is None and is_default and not is_delivery:
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
                    # lang="en": teeno reminder raste ek hi bhasha bolein —
                    # dashboard, staff panel, aur ye. Grahak ko pata nahi
                    # chalna chahiye ki kisne yaad dilaya.
                    text=get_message(
                        kind, lang="en", order_number=o.order_number, amount=f"{due}",
                    ),
                )
                sends += 1
            except SendError:
                log.info("payment_reminder_not_sent", order=o.order_number)
        if flagged and await _claim(f"payrem-adminflag:{now_ist.strftime('%Y-%m-%d')}"):
            try:
                await send_message(
                    db, to_phone=manager_phone(),
                    text=get_message("overdue_admin_flag", listing="\n".join(flagged[:15])),
                )
            except SendError:
                log.info("overdue_admin_flag_not_sent")
        await audit.record(
            actor_role="system", actor="scheduler", action="payment_reminders",
            args={}, result=f"sent {sends}, flagged {len(flagged)}",
        )
    return sends
