"""Marketing core: RFM segments, compliance gate, campaign send engine.

Compliance is CODE, not convention — every send passes eligible():
- opted_out / marketing_opt_out -> never
- frequency cap (Settings) -> skip
- complained in last 30 days -> skip
- quiet hours -> the whole campaign waits (resumed by the scheduler)
- monthly message budget (Settings) -> sends stop when hit
"""

import asyncio
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


async def queue_campaign(db: AsyncSession, campaign: Campaign) -> int:
    """Create recipient rows for the campaign's segment. Returns queued count."""
    segs = await compute_segments(db)
    targets = segs.get(campaign.segment, [])
    queued = 0
    for st in targets:
        ok, reason = await eligible(db, st["id"])
        rec = CampaignRecipient(
            campaign_id=campaign.id,
            customer_id=st["id"],
            status="queued" if ok else "skipped",
            detail=None if ok else reason,
        )
        db.add(rec)
        if ok:
            queued += 1
    await db.commit()
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

        budget = int(await app_settings.get(db, "marketing_monthly_msg_budget"))
        sent_now = 0
        while True:
            rec = (
                await db.execute(
                    select(CampaignRecipient)
                    .where(
                        CampaignRecipient.campaign_id == campaign.id,
                        CampaignRecipient.status == "queued",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if rec is None:
                break
            await db.refresh(campaign)
            if campaign.status == "cancelled":  # owner hit the brake mid-send
                log.info("campaign_cancelled_mid_send", campaign=str(campaign_id))
                return
            if _in_quiet_hours_now():
                log.info("campaign_paused_quiet_hours", campaign=str(campaign_id))
                return
            if await month_send_count(db) >= budget:
                rec.status = "skipped"
                rec.detail = "budget_hit"
                await db.commit()
                log.warning("campaign_budget_hit", campaign=str(campaign_id))
                continue
            cust = await db.get(Customer, rec.customer_id)
            text = campaign.message_text.replace("{name}", (cust.name or "ji") if cust else "ji")
            try:
                wamid = await send_message(db, to_phone=cust.phone, text=text)
                rec.status = "sent"
                rec.wa_message_id = wamid
                cust.last_marketing_at = datetime.now(timezone.utc)
                sent_now += 1
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


async def campaign_stats(db: AsyncSession, campaign_id) -> dict:
    rows = (
        await db.execute(
            select(CampaignRecipient.status, func.count())
            .where(CampaignRecipient.campaign_id == campaign_id)
            .group_by(CampaignRecipient.status)
        )
    ).all()
    stats = {status: count for status, count in rows}
    # revenue attribution: orders created within the window after a send
    window_days = int(await app_settings.get(db, "attribution_window_days"))
    attributed = (
        await db.execute(
            select(
                func.count(Order.id),
                func.coalesce(func.sum(Order.total_amount), 0),
            )
            .select_from(CampaignRecipient)
            .join(Order, Order.customer_id == CampaignRecipient.customer_id)
            .where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status.in_(("sent", "delivered", "read", "replied")),
                Order.created_at >= CampaignRecipient.updated_at,
                Order.created_at
                <= CampaignRecipient.updated_at + timedelta(days=window_days),
            )
        )
    ).one()
    stats["orders_attributed"] = attributed[0]
    stats["revenue_attributed"] = float(attributed[1])
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
