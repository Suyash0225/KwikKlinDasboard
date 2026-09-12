"""Marketing core: RFM segments, compliance gate, campaign send engine.

Compliance is CODE, not convention — every send passes eligible():
- opted_out / marketing_opt_out -> never
- frequency cap (Settings) -> skip
- complained in last 30 days -> skip
- quiet hours -> the whole campaign waits (resumed by the scheduler)
- monthly message budget (Settings) -> sends stop when hit
"""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models import (
    Campaign,
    CampaignRecipient,
    Customer,
    Escalation,
    Order,
)
from app.services import app_settings, audit
from app.services.whatsapp import SendError, send_message

log = structlog.get_logger()

SEGMENTS = ("new", "active_regular", "at_risk", "lapsed", "lost", "high_value", "outstanding_dues")


async def _customer_stats(db: AsyncSession) -> list[dict]:
    """One aggregate row per customer with orders — the RFM raw material."""
    rows = (
        await db.execute(
            select(
                Customer.id,
                Customer.phone,
                Customer.name,
                Customer.opted_out,
                Customer.marketing_opt_out,
                func.count(Order.id).label("order_count"),
                func.max(Order.created_at).label("last_order_at"),
                func.min(Order.created_at).label("first_order_at"),
                func.coalesce(func.sum(Order.amount_paid), 0).label("lifetime_paid"),
                func.coalesce(
                    func.sum(
                        func.greatest(
                            func.coalesce(Order.total_amount, 0) - Order.amount_paid, 0
                        )
                    ),
                    0,
                ).label("outstanding"),
            )
            .join(Order, Order.customer_id == Customer.id)
            .where(Customer.is_active)
            .group_by(Customer.id)
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def _classify(stat: dict, now: datetime, high_value_cutoff: Decimal) -> set[str]:
    segs: set[str] = set()
    last = stat["last_order_at"]
    first = stat["first_order_at"]
    age_days = (now - last).days if last else 9999
    if first and (now - first).days <= 30:
        segs.add("new")
    if stat["order_count"] >= 2 and age_days <= 60:
        segs.add("active_regular")
    if stat["order_count"] >= 2 and 30 < age_days <= 60:
        segs.add("at_risk")
    if 60 < age_days <= 120:
        segs.add("lapsed")
    if age_days > 120:
        segs.add("lost")
    if high_value_cutoff > 0 and Decimal(stat["lifetime_paid"]) >= high_value_cutoff:
        segs.add("high_value")
    if Decimal(stat["outstanding"]) > 0:
        segs.add("outstanding_dues")
    return segs


async def compute_segments(db: AsyncSession) -> dict[str, list[dict]]:
    """segment -> [customer stat dicts]. Live computation, no staleness."""
    stats = await _customer_stats(db)
    now = datetime.now(timezone.utc)
    paid_values = sorted((Decimal(s["lifetime_paid"]) for s in stats), reverse=True)
    cutoff = paid_values[max(0, len(paid_values) // 5 - 1)] if paid_values else Decimal("0")
    result: dict[str, list[dict]] = {s: [] for s in SEGMENTS}
    for st in stats:
        for seg in _classify(st, now, cutoff):
            result[seg].append(st)
    return result


async def recompute_segments(db: AsyncSession) -> dict[str, int]:
    """Nightly snapshot of counts (for the UI + weekly suggestion)."""
    segs = await compute_segments(db)
    counts = {k: len(v) for k, v in segs.items()}
    from app.models import SettingKV

    row = (
        await db.execute(select(SettingKV).where(SettingKV.key == "segments_snapshot"))
    ).scalar_one_or_none()
    payload = {"v": {"counts": counts, "at": datetime.now(timezone.utc).isoformat()}}
    if row is None:
        db.add(SettingKV(key="segments_snapshot", value=payload))
    else:
        row.value = payload
    await db.commit()
    log.info("segments_recomputed", **counts)
    return counts


async def eligible(db: AsyncSession, customer_id) -> tuple[bool, str]:
    """Compliance gate for ONE customer. Returns (ok, reason_if_not)."""
    cust = await db.get(Customer, customer_id)
    if cust is None or not cust.is_active:
        return False, "inactive"
    if cust.opted_out or cust.marketing_opt_out:
        return False, "opted_out"
    cap = int(await app_settings.get(db, "marketing_freq_cap_per_month"))
    if cust.last_marketing_at:
        min_gap_days = max(1, 30 // max(cap, 1))
        if datetime.now(timezone.utc) - cust.last_marketing_at < timedelta(days=min_gap_days):
            return False, "freq_cap"
    complained = (
        await db.execute(
            select(func.count())
            .select_from(Escalation)
            .where(
                Escalation.customer_id == customer_id,
                Escalation.question.like("COMPLAINT:%"),
                Escalation.created_at >= datetime.now(timezone.utc) - timedelta(days=30),
            )
        )
    ).scalar_one()
    if complained:
        return False, "recent_complaint"
    return True, ""


async def month_send_count(db: AsyncSession) -> int:
    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return (
        await db.execute(
            select(func.count())
            .select_from(CampaignRecipient)
            .where(
                CampaignRecipient.status.in_(("sent", "delivered", "read", "replied")),
                CampaignRecipient.updated_at >= month_start,
            )
        )
    ).scalar_one()


# Below this reach a holdout is pointless — you cannot read a lift signal
# out of two people, and you've silenced customers for nothing.
MIN_REACH_FOR_HOLDOUT = 20


def _is_holdout(campaign_id, customer_id, percent: int) -> bool:
    """Stable, campaign-specific coin flip.

    sha256 and not hash(): Python salts string hashing per process, so the
    same customer would land in the holdout on one worker and get the
    message on another — the measurement would be nonsense.
    """
    digest = hashlib.sha256(f"{campaign_id}:{customer_id}".encode()).digest()
    return (int.from_bytes(digest[:4], "big") % 100) < percent


async def queue_campaign(db: AsyncSession, campaign: Campaign) -> int:
    """Create recipient rows for the campaign's segment. Returns queued count.

    Eligible customers are split into 'queued' (get the message) and
    'holdout' (deliberately do NOT). Comparing the two afterwards is the
    only honest way to say a campaign earned anything — see campaign_stats.
    """
    segs = await compute_segments(db)
    targets = segs.get(campaign.segment, [])
    if not targets:
        return 0
    from app.services.leads import check_marketing_eligible_bulk

    # THE single gate: opt-out, cap, complaints, active order, bad rating —
    # for the whole segment in a fixed number of queries.
    verdicts = await check_marketing_eligible_bulk(db, [st["id"] for st in targets])
    eligible_ids = [st["id"] for st in targets if verdicts.get(st["id"], (False, ""))[0]]

    holdout_pct = int(await app_settings.get(db, "marketing_holdout_percent"))
    if len(eligible_ids) < MIN_REACH_FOR_HOLDOUT:
        holdout_pct = 0

    queued = 0
    for st in targets:
        ok, reason = verdicts.get(st["id"], (False, "unknown"))
        if not ok:
            status, detail = "skipped", reason
        elif holdout_pct and _is_holdout(campaign.id, st["id"], holdout_pct):
            status, detail = "holdout", "control group"
        else:
            status, detail = "queued", None
            queued += 1
        db.add(
            CampaignRecipient(
                campaign_id=campaign.id, customer_id=st["id"],
                status=status, detail=detail,
            )
        )
    await db.commit()
    log.info(
        "campaign_queued",
        campaign=campaign.name, eligible=len(eligible_ids),
        queued=queued, holdout=len(eligible_ids) - queued,
    )
    return queued


def _in_quiet_hours_now() -> bool:
    from app.services.scheduler import IST, _in_quiet_hours

    return _in_quiet_hours(datetime.now(IST))


async def send_campaign(campaign_id) -> None:
    """Worker: send to all queued recipients, ~1 msg/sec, budget-capped.

    Idempotent and resumable — only rows still in 'queued' are touched, so
    a crash/restart continues where it stopped (scheduler resumes it).
    """
    async with async_session_factory() as db:
        campaign = await db.get(Campaign, campaign_id)
        if campaign is None or campaign.status not in ("approved", "sending"):
            return
        if _in_quiet_hours_now():
            log.info("campaign_waiting_quiet_hours", campaign=str(campaign_id))
            return  # scheduler's hourly tick calls us again
        campaign.status = "sending"
        await db.commit()

        # Do chhatein: owner ka apna budget setting, AUR plan ka marketing cap.
        # Plan cap isliye zaroori hai ki marketing hi sabse mehngi cheez hai
        # (~₹0.80/msg) — bina cap ke ek client poore plan ka paisa jala de.
        budget = int(await app_settings.get(db, "marketing_monthly_msg_budget"))
        from app.models.tenant import Tenant as _T
        from app.services import plans as _plans
        from app.services import tenant_context as _tc

        _tid = _tc.current_tenant_id.get() or _tc.cached_home_tenant_id()
        if _tid is not None:
            _t = await db.get(_T, _tid)
            # effective_limits = plan + vendor override, ek hi jagah. Pehle
            # yahan override ka logic dobara likha tha — do jagah ka hisaab
            # kabhi na kabhi alag ho jaata hai.
            _cap = _plans.effective_limits(_t)["max_campaign_msgs_month"]
            if _cap is not None:
                budget = min(budget, _cap)
        # The month's spend is counted ONCE and then tracked locally. It used
        # to be a full COUNT over campaign_recipients before every single
        # message; re-syncing periodically keeps us honest when two
        # campaigns run at the same time, at 1/25th the cost.
        spent = await month_send_count(db)
        sent_now = 0
        while True:
            # Page the queue instead of re-querying one row at a time, and
            # bring each recipient's customer along — 'queued' is still the
            # only status we touch, so a crash mid-page resumes cleanly.
            page = (
                await db.execute(
                    select(CampaignRecipient, Customer)
                    .join(Customer, Customer.id == CampaignRecipient.customer_id)
                    .where(
                        CampaignRecipient.campaign_id == campaign.id,
                        CampaignRecipient.status == "queued",
                    )
                    .limit(50)
                )
            ).all()
            if not page:
                break
            for rec, cust in page:
                await db.refresh(campaign)
                if campaign.status == "cancelled":  # owner hit the brake mid-send
                    log.info("campaign_cancelled_mid_send", campaign=str(campaign_id))
                    return
                if _in_quiet_hours_now():
                    log.info("campaign_paused_quiet_hours", campaign=str(campaign_id))
                    return
                if sent_now and sent_now % 25 == 0:
                    spent = await month_send_count(db)
                if spent >= budget:
                    rec.status = "skipped"
                    rec.detail = "budget_hit"
                    await db.commit()
                    log.warning("campaign_budget_hit", campaign=str(campaign_id))
                    continue
                text = campaign.message_text.replace("{name}", cust.name or "ji")
                try:
                    # Campaign = MARKETING category: Meta par sabse mehnga
                    # (~₹0.80/msg) — billable meter isse alag ginta hai.
                    wamid = await send_message(
                        db, to_phone=cust.phone, text=text, category="marketing"
                    )
                    rec.status = "sent"
                    rec.wa_message_id = wamid
                    cust.last_marketing_at = datetime.now(timezone.utc)
                    sent_now += 1
                    spent += 1
                except SendError as exc:
                    # window closed & no approved marketing template -> honest fail
                    rec.status = "failed"
                    rec.detail = str(exc)[:200]
                await db.commit()
                await asyncio.sleep(1.0)  # Meta-friendly pace

        campaign.status = "sent"
        campaign.sent_at = datetime.now(timezone.utc)
        campaign.stats = await campaign_stats(db, campaign.id)
        await db.commit()
        await audit.record(
            actor_role="system", actor="marketing", action="campaign_sent",
            args={"campaign": campaign.name}, result=f"sent {sent_now}",
        )


SENT_STATUSES = ("sent", "delivered", "read", "replied")


async def _window_conversions(
    db: AsyncSession, campaign_id, statuses: tuple[str, ...], window_days: int
) -> tuple[int, int, int, float]:
    """(recipients, distinct converters, orders, revenue) for one group.

    Same measurement applied to the messaged group and the holdout, so the
    two are actually comparable.
    """
    recipients = (
        await db.execute(
            select(func.count())
            .select_from(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status.in_(statuses),
            )
        )
    ).scalar_one()
    if not recipients:
        return 0, 0, 0, 0.0
    converters, orders, revenue = (
        await db.execute(
            select(
                func.count(func.distinct(CampaignRecipient.customer_id)),
                func.count(Order.id),
                func.coalesce(func.sum(Order.total_amount), 0),
            )
            .select_from(CampaignRecipient)
            .join(Order, Order.customer_id == CampaignRecipient.customer_id)
            .where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status.in_(statuses),
                Order.created_at >= CampaignRecipient.updated_at,
                Order.created_at
                <= CampaignRecipient.updated_at + timedelta(days=window_days),
            )
        )
    ).one()
    return recipients, converters, orders, float(revenue)


async def campaign_stats(db: AsyncSession, campaign_id) -> dict:
    """Per-status counts, naive attribution, and TRUE incremental lift.

    Naive attribution ("they ordered after we messaged, so we caused it")
    flatters every campaign — regulars would have ordered anyway. The
    holdout group never got the message, so the gap between the two
    conversion rates is the part the campaign actually caused.
    """
    rows = (
        await db.execute(
            select(CampaignRecipient.status, func.count())
            .where(CampaignRecipient.campaign_id == campaign_id)
            .group_by(CampaignRecipient.status)
        )
    ).all()
    stats = {status: count for status, count in rows}

    window_days = int(await app_settings.get(db, "attribution_window_days"))
    sent_n, sent_conv, sent_orders, sent_rev = await _window_conversions(
        db, campaign_id, SENT_STATUSES, window_days
    )
    stats["orders_attributed"] = sent_orders
    stats["revenue_attributed"] = sent_rev

    hold_n, hold_conv, _, _ = await _window_conversions(
        db, campaign_id, ("holdout",), window_days
    )
    if sent_n and hold_n:
        rate_sent = sent_conv / sent_n
        rate_hold = hold_conv / hold_n
        lift = rate_sent - rate_hold
        # Extra customers this campaign produced beyond "they'd have come
        # anyway" — can be negative, and that is the useful case: it means
        # the messages annoyed more people than they moved.
        incremental_customers = lift * sent_n
        avg_order = (sent_rev / sent_orders) if sent_orders else 0.0
        stats["holdout_size"] = hold_n
        stats["conversion_sent"] = round(rate_sent, 4)
        stats["conversion_holdout"] = round(rate_hold, 4)
        stats["lift"] = round(lift, 4)
        stats["incremental_orders"] = round(incremental_customers, 1)
        stats["incremental_revenue"] = round(incremental_customers * avg_order, 2)
    return stats


async def track_status_update(db: AsyncSession, wa_message_id: str, status: str) -> None:
    """Called from the webhook statuses handler — delivery/read tracking."""
    if status not in ("delivered", "read"):
        return
    rec = (
        await db.execute(
            select(CampaignRecipient).where(
                CampaignRecipient.wa_message_id == wa_message_id
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        return
    # never downgrade read -> delivered
    if rec.status == "read" and status == "delivered":
        return
    rec.status = status
    await db.commit()


async def track_reply(db: AsyncSession, customer_id) -> None:
    """Inbound from a recently-messaged campaign recipient -> 'replied'."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    rec = (
        await db.execute(
            select(CampaignRecipient)
            .where(
                CampaignRecipient.customer_id == customer_id,
                CampaignRecipient.status.in_(("sent", "delivered", "read")),
                CampaignRecipient.updated_at >= cutoff,
            )
            .order_by(CampaignRecipient.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if rec is not None:
        rec.status = "replied"
        await db.commit()
