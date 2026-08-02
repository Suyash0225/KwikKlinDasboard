"""Scheduler (standup/reminders) + marketing (segments/campaigns/coupons).

All WhatsApp sends recorded via monkeypatch; quiet hours forced open so
tests pass at any wall-clock time; sent_events keys cleaned up.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text as sqltext

import app.services.marketing as marketing_module
import app.services.marketing_agent as magent_module
import app.services.scheduler as sched_module
from app.database import async_session_factory
from app.models import Campaign, CampaignRecipient, Coupon, Customer, Order, OrderStatus
from app.services import app_settings
from app.services.marketing import (
    compute_segments,
    eligible,
    queue_campaign,
    send_campaign,
)
from app.services.marketing_agent import redeem_coupon, validate_coupon
from app.services.order_service import create_order
from app.services.scheduler import run_payment_reminders, run_standup

PHONE = "+919999900321"
RAVI_PHONE = "+918707093136"


@pytest.fixture(autouse=True)
async def _setup_cleanup(monkeypatch):
    # quiet hours never block tests
    monkeypatch.setattr(sched_module, "_in_quiet_hours", lambda now: False)
    yield
    from tests.conftest import purge_phones

    await purge_phones(PHONE)
    async with async_session_factory() as s:
        await s.execute(sqltext("DELETE FROM sent_events WHERE event_key LIKE '%TEST%' OR event_key LIKE 'standup:%' OR event_key LIKE 'duenudge:%' OR event_key LIKE 'payrem%'"))
        await s.execute(sqltext("DELETE FROM campaign_recipients WHERE campaign_id IN (SELECT id FROM campaigns WHERE name LIKE 'test-%')"))
        await s.execute(sqltext("DELETE FROM coupon_redemptions WHERE coupon_code LIKE 'TEST%'"))
        await s.execute(sqltext("DELETE FROM coupons WHERE code LIKE 'TEST%'"))
        await s.execute(sqltext("DELETE FROM campaigns WHERE name LIKE 'test-%'"))
        await s.execute(sqltext("DELETE FROM settings_kv WHERE key = 'default_washer_phone'"))
        await s.commit()


@pytest.fixture
def sched_sent(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_send(db, *, to_phone, text=None, **kw):
        calls.append({"to": to_phone, "text": text, **kw})
        return f"wamid.SCHEDTEST-{len(calls)}"

    monkeypatch.setattr(sched_module, "send_message", fake_send)
    monkeypatch.setattr(marketing_module, "send_message", fake_send)
    monkeypatch.setattr(magent_module, "send_message", fake_send)
    return calls


async def _seed_order(**kw) -> Order:
    async with async_session_factory() as db:
        return await create_order(
            db, customer_phone=PHONE, customer_name="Sched Grahak",
            items=[{"type": "Kurta", "qty": 1}], created_by="test", **kw,
        )


async def test_standup_lists_pending_and_is_idempotent(sched_sent, sent) -> None:
    async with async_session_factory() as db:
        await app_settings.set_value(db, "default_washer_phone", RAVI_PHONE)
    order = await _seed_order()
    n1 = await run_standup(force=True)
    assert n1 >= 1
    ravi_msgs = [c for c in sched_sent if c["to"] == RAVI_PHONE]
    assert ravi_msgs and order.order_number in ravi_msgs[0]["text"]
    # same day again -> claimed keys block resend
    before = len(sched_sent)
    n2 = await run_standup(force=True)
    assert n2 == 0 and len(sched_sent) == before


async def test_payment_reminders_polite_then_firm(sched_sent, sent) -> None:
    order = await _seed_order(total_amount=Decimal("500"))
    async with async_session_factory() as s:
        row = (
            await s.execute(select(Order).where(Order.order_number == order.order_number))
        ).scalar_one()
        row.status = OrderStatus.DELIVERED
        row.actual_delivery = datetime.now(timezone.utc) - timedelta(days=4)
        await s.commit()
    await run_payment_reminders()
    mine = [c for c in sched_sent if c["to"] == PHONE]
    assert mine and "500" in mine[0]["text"]

    # age it to firm territory -> firmer message + admin flag
    async with async_session_factory() as s:
        row = (
            await s.execute(select(Order).where(Order.order_number == order.order_number))
        ).scalar_one()
        row.actual_delivery = datetime.now(timezone.utc) - timedelta(days=20)
        await s.commit()
    await run_payment_reminders()
    texts = [c["text"] for c in sched_sent]
    assert any("kaafi dino" in t for t in texts)
    admin_msgs = [c for c in sched_sent if c["to"] != PHONE and c["to"] != RAVI_PHONE]
    assert any("udhaar" in (c["text"] or "").lower() for c in admin_msgs)


async def test_segments_classify_lapsed_and_dues(sent) -> None:
    order = await _seed_order(total_amount=Decimal("300"))
    async with async_session_factory() as s:
        row = (
            await s.execute(select(Order).where(Order.order_number == order.order_number))
        ).scalar_one()
        row.created_at = datetime.now(timezone.utc) - timedelta(days=75)
        await s.commit()
    async with async_session_factory() as db:
        segs = await compute_segments(db)
    lapsed_phones = {m["phone"] for m in segs["lapsed"]}
    dues_phones = {m["phone"] for m in segs["outstanding_dues"]}
    assert PHONE in lapsed_phones and PHONE in dues_phones


async def test_eligibility_blocks_opted_out(sent) -> None:
    await _seed_order()
    async with async_session_factory() as s:
        cust = (
            await s.execute(select(Customer).where(Customer.phone == PHONE))
        ).scalar_one()
        cust.marketing_opt_out = True
        await s.commit()
        ok, reason = await eligible(s, cust.id)
    assert ok is False and reason == "opted_out"


async def test_campaign_queue_send_and_track(sched_sent, sent, monkeypatch) -> None:
    # make the pacer instant
    import asyncio as aio

    real_sleep = aio.sleep
    monkeypatch.setattr(marketing_module.asyncio, "sleep", lambda s: real_sleep(0))

    order = await _seed_order(total_amount=Decimal("300"))
    async with async_session_factory() as s:
        row = (
            await s.execute(select(Order).where(Order.order_number == order.order_number))
        ).scalar_one()
        row.created_at = datetime.now(timezone.utc) - timedelta(days=75)
        await s.commit()

    async with async_session_factory() as db:
        campaign = Campaign(
            name="test-winback", segment="lapsed",
            message_text="Namaste {name}! 10% off — Kwik Klin",
            status="approved", created_by="test",
        )
        db.add(campaign)
        await db.commit()
        queued = await queue_campaign(db, campaign)
        cid = campaign.id
    assert queued >= 1
    await send_campaign(cid)
    my_sends = [c for c in sched_sent if c["to"] == PHONE]
    assert my_sends and "Sched Grahak" in my_sends[0]["text"]

    async with async_session_factory() as s:
        rec = (
            await s.execute(
                select(CampaignRecipient)
                .join(Customer, Customer.id == CampaignRecipient.customer_id)
                .where(CampaignRecipient.campaign_id == cid, Customer.phone == PHONE)
            )
        ).scalar_one()
        assert rec.status == "sent"
        camp = await s.get(Campaign, cid)
        assert camp.status == "sent"
        # delivery receipt upgrades the row
        from app.services.marketing import track_status_update

        await track_status_update(s, rec.wa_message_id, "read")
        await s.refresh(rec)
        assert rec.status == "read"


async def test_coupon_validate_and_redeem(sent) -> None:
    order = await _seed_order(total_amount=Decimal("400"))
    async with async_session_factory() as db:
        db.add(
            Coupon(code="TESTOFF10", discount_type="percent", value=Decimal("10"),
                   min_order=Decimal("100"))
        )
        await db.commit()
        cust = (
            await db.execute(select(Customer).where(Customer.phone == PHONE))
        ).scalar_one()
        coupon, discount, err = await validate_coupon(db, "testoff10", cust.id, Decimal("400"))
        assert err == "" and discount == Decimal("40.00")
        row = (
            await db.execute(select(Order).where(Order.order_number == order.order_number))
        ).scalar_one()
        await redeem_coupon(db, coupon, row, discount)
        # second use blocked by per-customer limit
        coupon2, d2, err2 = await validate_coupon(db, "TESTOFF10", cust.id, Decimal("400"))
        assert coupon2 is None and "use kar chuke" in err2
