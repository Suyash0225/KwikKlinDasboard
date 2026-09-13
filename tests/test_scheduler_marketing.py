"""Scheduler (standup/reminders) + marketing (segments/campaigns/coupons).

All WhatsApp sends recorded via monkeypatch; quiet hours forced open so
tests pass at any wall-clock time; sent_events keys cleaned up.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text as sqltext

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
from tests.conftest import TEST_WASHER_PHONE

PHONE = "+919999900321"


@pytest.fixture(autouse=True)
async def _setup_cleanup(monkeypatch):
    # quiet hours never block tests
    monkeypatch.setattr(sched_module, "_in_quiet_hours", lambda now: False)
    # owner ki asli setting yaad rakho — test isse overwrite karta hai.
    # app_settings.get() se padho, raw SQL se nahi: column mein value
    # {'v': ...} shape mein hai, aur raw dict wapas likhne par har run ek
    # aur layer chadha deta tha.
    async with async_session_factory() as s:
        prior = await app_settings.get(s, "default_washer_phone")
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
    if prior:
        async with async_session_factory() as s:
            await app_settings.set_value(s, "default_washer_phone", prior)


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


async def test_standup_lists_pending_and_is_idempotent(sched_sent, sent, test_washer) -> None:
    async with async_session_factory() as db:
        await app_settings.set_value(db, "default_washer_phone", TEST_WASHER_PHONE)
    order = await _seed_order()
    n1 = await run_standup(force=True)
    assert n1 >= 1
    washer_msgs = [c for c in sched_sent if c["to"] == TEST_WASHER_PHONE]
    assert washer_msgs and order.order_number in washer_msgs[0]["text"]
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
    # Reminder ab angrezi mein jaate hain (dashboard/panel/scheduler teeno
    # ek bhasha), isliye "kaafi dino" ki jagah uska en variant.
    assert any("pending for a while" in t for t in texts)
    admin_msgs = [c for c in sched_sent if c["to"] != PHONE and c["to"] != TEST_WASHER_PHONE]
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
        # a real lapsed customer has no ACTIVE order — the single gate
        # (check_marketing_eligible) rightly skips anyone mid-order
        row.status = OrderStatus.DELIVERED
        row.actual_delivery = datetime.now(timezone.utc) - timedelta(days=74)
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


HOLDOUT_PHONES = [f"+9199999031{i:02d}" for i in range(30)]


async def _seed_lapsed_crowd(phones: list[str], paid: str = "300") -> None:
    """A crowd that is genuinely lapsed: delivered order, 75 days cold."""
    for i, phone in enumerate(phones):
        async with async_session_factory() as db:
            order = await create_order(
                db, customer_phone=phone, customer_name=f"Grahak {i}",
                items=[{"type": "Kurta", "qty": 1}], created_by="test",
                total_amount=Decimal(paid),
            )
        async with async_session_factory() as s:
            row = (
                await s.execute(select(Order).where(Order.order_number == order.order_number))
            ).scalar_one()
            row.created_at = datetime.now(timezone.utc) - timedelta(days=75)
            row.status = OrderStatus.DELIVERED
            row.actual_delivery = datetime.now(timezone.utc) - timedelta(days=74)
            row.amount_paid = Decimal(paid)
            await s.commit()


async def test_holdout_group_is_carved_out_and_never_messaged(
    sched_sent, sent, monkeypatch
) -> None:
    """A control group is what makes attribution honest — so it must never
    receive the campaign, and it must stay the same group on every run."""
    import asyncio as aio

    real_sleep = aio.sleep
    monkeypatch.setattr(marketing_module.asyncio, "sleep", lambda s: real_sleep(0))
    try:
        await _seed_lapsed_crowd(HOLDOUT_PHONES)
        async with async_session_factory() as db:
            await app_settings.set_value(db, "marketing_holdout_percent", 20)
            campaign = Campaign(
                name="test-holdout", segment="lapsed",
                message_text="Namaste {name}! — Kwik Klin",
                status="approved", created_by="test",
            )
            db.add(campaign)
            await db.commit()
            queued = await queue_campaign(db, campaign)
            cid = campaign.id

        async with async_session_factory() as s:
            held = (
                await s.execute(
                    select(Customer.phone)
                    .join(CampaignRecipient, CampaignRecipient.customer_id == Customer.id)
                    .where(
                        CampaignRecipient.campaign_id == cid,
                        CampaignRecipient.status == "holdout",
                    )
                )
            ).scalars().all()
        assert held, "20% holdout on a 30-person segment should not be empty"
        assert queued >= 1

        await send_campaign(cid)
        messaged = {c["to"] for c in sched_sent}
        assert not (messaged & set(held)), "holdout customers were messaged"

        # same campaign + same customer -> same side of the split, always
        from app.services.marketing import _is_holdout

        async with async_session_factory() as s:
            one = (
                await s.execute(select(Customer).where(Customer.phone == held[0]))
            ).scalar_one()
        assert all(_is_holdout(cid, one.id, 20) for _ in range(3))
    finally:
        from tests.conftest import purge_phones

        await purge_phones(*HOLDOUT_PHONES)
        async with async_session_factory() as s:
            await s.execute(
                sqltext("DELETE FROM settings_kv WHERE key = 'marketing_holdout_percent'")
            )
            await s.commit()


async def test_tiny_segment_gets_no_holdout(sent) -> None:
    """Silencing 1 of 4 customers buys no signal — just less reach."""
    await _seed_lapsed_crowd(HOLDOUT_PHONES[:4])
    async with async_session_factory() as db:
        await app_settings.set_value(db, "marketing_holdout_percent", 20)
    async with async_session_factory() as db:
        campaign = Campaign(
            name="test-tiny", segment="lapsed", message_text="hi {name}",
            status="approved", created_by="test",
        )
        db.add(campaign)
        await db.commit()
        await queue_campaign(db, campaign)
        held = (
            await db.execute(
                select(func.count())
                .select_from(CampaignRecipient)
                .where(
                    CampaignRecipient.campaign_id == campaign.id,
                    CampaignRecipient.status == "holdout",
                )
            )
        ).scalar_one()
    assert held == 0
    from tests.conftest import purge_phones

    await purge_phones(*HOLDOUT_PHONES[:4])
    async with async_session_factory() as s:
        await s.execute(
            sqltext("DELETE FROM settings_kv WHERE key = 'marketing_holdout_percent'")
        )
        await s.commit()


async def test_agent_chases_the_biggest_opportunity_not_the_playbook_order(
    sent,
) -> None:
    """The old picker took the first playbook entry with 3+ members, so a
    3-person lapsed group beat a 25-person one. It now ranks by rupees."""
    from app.services.marketing_agent import _pick_segment

    await _seed_lapsed_crowd(HOLDOUT_PHONES[:25], paid="800")
    try:
        async with async_session_factory() as db:
            pick = await _pick_segment(db)
        assert pick is not None
        assert pick["segment"] == "lapsed"
        assert pick["reach"] >= 25
        assert pick["score"] > 0
        # the rationale the owner reads must show the maths, not just a verdict
        from app.services.marketing_agent import _rationale

        why = _rationale(pick)
        assert "reply rate" in why and "expected business" in why
    finally:
        from tests.conftest import purge_phones

        await purge_phones(*HOLDOUT_PHONES[:25])


async def test_standup_goes_out_as_a_tappable_list(sched_sent, sent, test_washer) -> None:
    """Roz subah ki list bhi chun-ne layak ho.

    Plain text list mein staff ko likhna padta tha aur "kis order ka jawab"
    hamesha saaf nahi hota tha. Ab har order apni line par hai.
    """
    async with async_session_factory() as db:
        await app_settings.set_value(db, "default_washer_phone", TEST_WASHER_PHONE)
    order = await _seed_order()
    await run_standup(force=True)

    msg = next(c for c in sched_sent if c["to"] == TEST_WASHER_PHONE)
    rows = msg.get("list_rows") or []
    assert any(r.id == f"pick:o:{order.order_number}" for r in rows), \
        "standup list mein order tappable hona chahiye"
    assert msg.get("list_button") == "Kaam chuniye"


async def test_delivery_staff_never_gets_wash_work(sched_sent, sent) -> None:
    """Asli galti (09 Aug): Ajit delivery ka aadmi hai, par uske naam par
    likha order abhi RECEIVED tha — aur standup ne use dhulai wali list
    bhej di ("kaun se ho gaye?"). Uske liye us waqt kaam tha hi nahi.
    """
    from app.database import async_session_factory as _asf
    from app.models import Staff, StaffRole
    from app.services.scheduler import _pending_orders_for

    phone = "+919999900091"
    async with _asf() as db:
        db.add(Staff(phone=phone, name="Deliverywala", role=StaffRole.DELIVERY, is_active=True))
        await db.commit()
    try:
        order = await _seed_order()          # RECEIVED — abhi dhulai baaki hai
        async with _asf() as db:
            row = (
                await db.execute(select(Order).where(Order.order_number == order.order_number))
            ).scalar_one()
            st = (
                await db.execute(select(Staff).where(Staff.phone == phone))
            ).scalar_one()
            row.assigned_delivery_id = st.id
            await db.commit()

            mine = await _pending_orders_for(db, st, "")
            assert order.order_number not in [o.order_number for o in mine], \
                "delivery wale ko dhulai ka kaam nahi dikhna chahiye"

            # kapde taiyar hote hi wahi order uske paas aa jata hai
            row = (
                await db.execute(select(Order).where(Order.order_number == order.order_number))
            ).scalar_one()
            row.status = OrderStatus.READY
            await db.commit()
            mine2 = await _pending_orders_for(db, st, "")
        assert order.order_number in [o.order_number for o in mine2]
    finally:
        async with _asf() as db:
            await db.execute(sqltext(
                "UPDATE orders SET assigned_delivery_id = NULL WHERE assigned_delivery_id IN"
                " (SELECT id FROM staff WHERE phone = :p)"), {"p": phone})
            await db.execute(sqltext("DELETE FROM staff WHERE phone = :p"), {"p": phone})
            await db.commit()


async def test_standup_greeting_follows_the_clock(sched_sent, sent, test_washer) -> None:
    """Sham 6 baje "Good morning" bot ko bewakoof dikhata hai."""
    from datetime import datetime

    from app.services.scheduler import IST, _greeting

    assert "morning" in _greeting(datetime(2026, 8, 9, 9, 0, tzinfo=IST)).lower()
    assert "evening" in _greeting(datetime(2026, 8, 9, 18, 0, tzinfo=IST)).lower()
    assert "morning" not in _greeting(datetime(2026, 8, 9, 18, 0, tzinfo=IST)).lower()
