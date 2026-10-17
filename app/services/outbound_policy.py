"""Shared tenant-aware outbound policy for WhatsApp customer messages.

Call this from the transport gateway with an explicit intent. Internal owner/staff
messages remain outside customer marketing policy; customer sends without tenant
identity fail closed rather than falling back to the deployment's home shop.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer, OutboundThrottle
from app.services import app_settings
from app.services.tenant_context import current_tenant_id
from app.services.whatsapp import SendError, _blocked_test_recipient

log = structlog.get_logger()
IST = ZoneInfo("Asia/Kolkata")
PROMOTIONAL = "promotional"
TRANSACTIONAL = "transactional"


def _quiet_now(now: datetime | None = None) -> bool:
    from app.services.scheduler import _in_quiet_hours

    current = (now or datetime.now(IST)).astimezone(IST)
    return _in_quiet_hours(current)


async def _reserve_promotional_slot(db: AsyncSession, tenant_id, now: datetime) -> None:
    """Atomically reserve a tenant daily-cap slot and a cross-worker send slot.

    The row lock is held only while reserving; the reservation is committed
    before waiting/sending so concurrent workers cannot take the same slot.
    Failed sends still consume a slot, which is conservative and avoids bursts.
    """
    from app.models.event import OutboundThrottle

    cap = max(1, int(await app_settings.get(db, "outbound_daily_msg_cap") or 100))
    today = now.astimezone(IST).date()
    await db.execute(
        pg_insert(OutboundThrottle)
        .values(
            tenant_id=tenant_id,
            daily_date=today,
            daily_count=0,
            next_allowed_at=now,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    row = (
        await db.execute(
            select(OutboundThrottle)
            .where(OutboundThrottle.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one()
    if row.daily_date != today:
        row.daily_date = today
        row.daily_count = 0
        row.next_allowed_at = now
    if row.daily_count >= cap:
        await db.commit()
        raise SendError(f"promotional daily cap reached ({cap})", transient=False)

    slot_at = max(now, row.next_allowed_at or now)
    row.daily_count += 1
    row.next_allowed_at = slot_at + timedelta(seconds=random.uniform(15.0, 20.0))
    await db.commit()
    delay = (slot_at - datetime.now(now.tzinfo)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)


async def enforce_outbound_policy(
    db: AsyncSession,
    *,
    customer: Customer | None,
    phone: str,
    intent: str,
    kind: str | None = None,
    urgent: bool = False,
) -> None:
    """Enforce the policy immediately before a customer-facing outbound send.

    Promotional: marketing.eligible + quiet hours + tenant daily cap + shared
    15–20s pacing + hard test-number block.
    Transactional: active/customer STOP opt-out + opt-in for kind-specific
    reminders + test-number block + quiet hours unless explicitly urgent.
    """
    if intent not in {PROMOTIONAL, TRANSACTIONAL}:
        raise SendError("outbound intent must be promotional or transactional", transient=False)

    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        raise SendError("outbound blocked: tenant context missing", transient=False)
    if _blocked_test_recipient(phone):
        raise SendError("automated send blocked for test recipient", transient=False)
    if customer is None:
        raise SendError("outbound blocked: recipient is not a tenant customer", transient=False)
    if customer.tenant_id != tenant_id:
        raise SendError("outbound blocked: recipient tenant mismatch", transient=False)
    if intent == PROMOTIONAL:
        if not customer.is_active:
            raise SendError("promotional send blocked: customer inactive", transient=False)
        from app.services.marketing import eligible

        ok, reason = await eligible(db, customer.id)
        if not ok:
            raise SendError(f"promotional send blocked: {reason}", transient=False)
        if _quiet_now():
            raise SendError("promotional send blocked: quiet hours", transient=False)
        await _reserve_promotional_slot(db, tenant_id, datetime.now(IST))
        return

    # STOP is global and always wins; a marketing-only opt-out does not
    # suppress a legitimate utility update.
    if customer.opted_out:
        raise SendError("transactional send blocked: customer opted out", transient=False)
    if kind in {"payment_reminder", "payment_reminder_firm"} and not customer.payment_reminders_enabled:
        raise SendError("transactional send blocked: payment reminders not enabled", transient=False)
    if not urgent and _quiet_now():
        raise SendError("transactional send blocked: quiet hours", transient=False)
