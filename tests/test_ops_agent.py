"""IMP_007 + IMP_006: agent khud kaam baante/sambhale, aur deri pakde.

Alag test dukaan mein — asli dukaan ke staff par kabhi kaam na jaye.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text as sqltext

from app.database import async_session_factory
from app.models import Order, OrderStatus, Staff, StaffRole, Task
from app.services import app_settings, ops_agent, order_service, tasks as task_service, tenant_context, turnaround
from tests.test_staff_panel import _purge_staff, _staff, _tenant, purge_phones

SLUG = "ops-agent-shop"
W1, W2, D1 = "+919999900291", "+919999900292", "+919999900293"
CUST = "+919999900294"


@pytest.fixture
async def shop(sent):
    await _purge_staff(W1, W2, D1)
    tid = await _tenant(SLUG, "pro")
    ids = {
        "t": tid,
        "w1": await _staff(tid, W1, "Opswash One", StaffRole.WASHER),
        "w2": await _staff(tid, W2, "Opswash Two", StaffRole.WASHER),
        "d1": await _staff(tid, D1, "Opsboy", StaffRole.DELIVERY),
    }
    tok = tenant_context.current_tenant_id.set(tid)
    try:
        yield ids
    finally:
        tenant_context.current_tenant_id.reset(tok)
        await purge_phones(CUST)
        await _purge_staff(W1, W2, D1)
        async with async_session_factory() as db:
            for q in ("DELETE FROM tasks WHERE tenant_id = :t", "DELETE FROM settings_kv WHERE tenant_id = :t",
                      "DELETE FROM audit_log WHERE tenant_id = :t", "DELETE FROM tenants WHERE id = :t"):
                await db.execute(sqltext(q), {"t": str(tid)})
            await db.commit()


async def _order(**kw) -> Order:
    async with async_session_factory() as db:
        return await order_service.create_order(
            db, customer_phone=CUST, customer_name="Ops Grahak",
            items=[{"type": "Shirt", "qty": 3}], total_amount=Decimal("150"),
            created_by="test", **kw,
        )


async def _tasks(order_id) -> list[Task]:
    async with async_session_factory() as db:
        return list((await db.execute(
            select(Task).where(Task.order_id == order_id).order_by(Task.created_at)
        )).scalars().all())


async def test_bill_creates_wash_task_for_the_least_busy_washer(shop) -> None:
    # Washer One ke paas pehle se ek khula kaam hai -> naya kaam Two ko
    async with async_session_factory() as db:
        w1 = await db.get(Staff, shop["w1"])
        await task_service.create_task(db, title="purana kaam", staff=w1, notify=False)

    o = await _order()
    ts = await _tasks(o.id)
    assert [t.kind for t in ts] == ["wash"]
    assert ts[0].assigned_staff_id == shop["w2"] and ts[0].created_by == "agent"
    async with async_session_factory() as db:
        assert (await db.get(Order, o.id)).assigned_washer_id == shop["w2"]


async def test_wash_task_waits_until_three_days_before_delivery(shop, sent) -> None:
    future = date.today() + timedelta(days=8)
    o = await _order(expected_delivery=future)
    assert await _tasks(o.id) == []

    async with async_session_factory() as db:
        created = await ops_agent.plan_due_wash_tasks(db, today=date.today() + timedelta(days=4))
        assert created == 0
        assert await ops_agent.plan_due_wash_tasks(db, today=date.today() + timedelta(days=4)) == 0

        created = await ops_agent.plan_due_wash_tasks(db, today=date.today() + timedelta(days=5))
        assert created == 1

    ts = await _tasks(o.id)
    assert len(ts) == 1 and ts[0].kind == "wash"
    assert ts[0].status == "OPEN"
    assert any(x["to"] in (W1, W2) for x in sent)


async def test_pickup_bill_gives_pickup_task_to_delivery_boy_without_double_message(shop, sent) -> None:
    o = await _order(needs_pickup=True, pickup_date=date.today())
    kinds = {t.kind: t for t in await _tasks(o.id)}
    assert set(kinds) == {"wash", "pickup"}
    assert kinds["pickup"].assigned_staff_id == shop["d1"]
    to_boy = [c for c in sent if c["to"] == D1]
    assert len(to_boy) == 1 and "Kab tak" in (to_boy[0]["text"] or ""), to_boy


async def test_wash_dry_iron_pipeline_hands_delivery_to_boy(shop) -> None:
    o = await _order()
    wash = (await _tasks(o.id))[0]

    async with async_session_factory() as db:
        t = await db.get(Task, wash.id)
        await task_service.complete_task(db, t, by="Opswash Two")

    async with async_session_factory() as db:
        assert (await db.get(Order, o.id)).status is OrderStatus.IN_DRY
    kinds = {t.kind: t for t in await _tasks(o.id)}
    assert kinds["wash"].status == "DONE"
    assert kinds["dry"].status == "OPEN"

    async with async_session_factory() as db:
        t = await db.get(Task, kinds["dry"].id)
        await task_service.complete_task(db, t, by="Opswash Two")

    async with async_session_factory() as db:
        assert (await db.get(Order, o.id)).status is OrderStatus.IN_IRON
    kinds = {t.kind: t for t in await _tasks(o.id)}
    assert kinds["dry"].status == "DONE"
    assert kinds["iron"].status == "OPEN"

    async with async_session_factory() as db:
        t = await db.get(Task, kinds["iron"].id)
        await task_service.complete_task(db, t, by="Opswash Two")

    async with async_session_factory() as db:
        assert (await db.get(Order, o.id)).status is OrderStatus.READY
    kinds = {t.kind: t for t in await _tasks(o.id)}
    assert kinds["iron"].status == "DONE"
    assert kinds["delivery"].status == "OPEN" and kinds["delivery"].assigned_staff_id == shop["d1"]

    async with async_session_factory() as db:
        order = await db.get(Order, o.id)
        await order_service.update_status(db, order, OrderStatus.OUT_FOR_DELIVERY, changed_by="t")
        await order_service.update_status(db, order, OrderStatus.DELIVERED, changed_by="t")
    assert all(t.status == "DONE" for t in await _tasks(o.id))


async def test_cancel_stops_open_work_and_switch_off_means_no_tasks(shop) -> None:
    o = await _order()
    async with async_session_factory() as db:
        await order_service.update_status(db, await db.get(Order, o.id), OrderStatus.CANCELLED, changed_by="t")
    assert [t.status for t in await _tasks(o.id)] == ["CANCELLED"]

    async with async_session_factory() as db:
        await app_settings.set_value(db, "agent_auto_assign", False)
    o2 = await _order()
    assert await _tasks(o2.id) == []


async def test_rebalance_moves_work_off_a_removed_washer(shop) -> None:
    o = await _order()
    wash = (await _tasks(o.id))[0]
    old = wash.assigned_staff_id
    async with async_session_factory() as db:
        await db.execute(sqltext("UPDATE staff SET is_active = false WHERE id = :i"), {"i": str(old)})
        await db.commit()
        assert await ops_agent.rebalance(db) == 1
    moved = (await _tasks(o.id))[0]
    assert moved.assigned_staff_id not in (None, old)
    async with async_session_factory() as db:
        assert (await db.get(Order, o.id)).assigned_washer_id == moved.assigned_staff_id


def _h(status, hours_ago, now):
    return SimpleNamespace(new_status=status, changed_at=now - timedelta(hours=hours_ago), changed_by="t")


def test_track_flags_stage_and_promise_delays_with_milestones() -> None:
    now = datetime.now(timezone.utc)
    order = SimpleNamespace(
        status=OrderStatus.IN_WASH, created_at=now - timedelta(hours=40), actual_delivery=None,
        expected_delivery=date.today() - timedelta(days=1), bill_seconds=95,
    )
    hist = [_h(OrderStatus.RECEIVED, 40, now), _h(OrderStatus.IN_WASH, 30, now)]
    t = turnaround.track(order, hist, {"RECEIVED": 6, "IN_WASH": 24}, now)
    assert t["stage_late"] and t["promise_late"] and t["delayed"]
    assert t["stage_hours"] == 30.0 and t["stage_limit"] == 24
    assert "Washing for 30h" in t["delay_reason"]
    assert [m["hours"] for m in t["milestones"]] == [10.0, 30.0]
    assert t["milestones"][0]["late"] is True and t["bill_seconds"] == 95

    fine = SimpleNamespace(**{**order.__dict__, "expected_delivery": date.today() + timedelta(days=2)})
    t2 = turnaround.track(fine, [_h(OrderStatus.IN_WASH, 2, now)], {"IN_WASH": 24}, now)
    assert not t2["delayed"]

    stage_only = SimpleNamespace(**{**fine.__dict__, "expected_delivery": date.today() + timedelta(days=2)})
    t3 = turnaround.track(stage_only, [_h(OrderStatus.IN_WASH, 30, now)], {"IN_WASH": 24}, now)
    assert t3["stage_late"] is True
    assert t3["promise_late"] is False
    assert t3["delayed"] is False

    assert turnaround.clean_bill_seconds("45") == 45 and turnaround.clean_bill_seconds(99999) is None


def test_deadline_reminder_cadence_gets_tighter() -> None:
    now = datetime.now(timezone.utc)
    now_ist = now.astimezone(task_service.IST)
    task = SimpleNamespace(urgent=False)
    order = SimpleNamespace(expected_delivery=now_ist.date() + timedelta(days=3))
    assert task_service._task_ping_gap_hours(task, order, now_ist) == 4
    order.expected_delivery = now_ist.date() + timedelta(days=2)
    assert task_service._task_ping_gap_hours(task, order, now_ist) == 2
    order.expected_delivery = now_ist.date() + timedelta(days=1)
    assert task_service._task_ping_gap_hours(task, order, now_ist) == 1
    order.expected_delivery = now_ist.date()
    assert task_service._task_ping_gap_hours(task, order, now_ist) == 1


async def test_delay_alert_goes_once_per_order_stage(shop, sent) -> None:
    o = await _order()
    async with async_session_factory() as db:
        await db.execute(sqltext(
            "UPDATE order_status_history SET changed_at = now() - interval '30 hours' WHERE order_id = :i"),
            {"i": str(o.id)})
        await db.commit()
        assert await turnaround.run_delay_alerts(db) == 1
        assert await turnaround.run_delay_alerts(db) == 0


async def test_dashboard_and_order_detail_carry_tracking_and_bill_time(client, shop) -> None:
    from app.config import settings

    o = await _order(bill_seconds=75)
    async with async_session_factory() as db:
        assert (await db.get(Order, o.id)).bill_seconds == 75
        tracked = await turnaround.track_many(db, [await db.get(Order, o.id)])
    t = tracked[o.id]
    assert t["stage"] == "RECEIVED" and t["bill_seconds"] == 75 and len(t["milestones"]) == 1
