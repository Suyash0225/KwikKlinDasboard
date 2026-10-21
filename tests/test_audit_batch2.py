"""Audit batch 2 — dohra paisa, hold se wapas aaya order, reassign."""

from decimal import Decimal

from sqlalchemy import select

from app.database import async_session_factory
from app.models import Order, OrderStatus, Payment, Staff, Task
from app.models.task import TASK_OPEN
from app.services import order_service, tasks as task_service, tenant_context
from tests.test_staff_panel import (  # noqa: F401 (two_shops fixture yahin se aata hai)
    A_DEL_PHONE, CUST_A, _login, _order_for, two_shops,
)
from tests.test_staff_tools import _set_status


async def test_collect_same_client_ref_records_money_once(client, two_shops, sent) -> None:
    num = await _order_for(two_shops["a"], two_shops["a_del"], CUST_A, delivery=True, total=300)
    await _set_status(two_shops["a"], num, "OUT_FOR_DELIVERY")
    await _login(client, A_DEL_PHONE)
    body = {"amount": 100, "method": "cash", "client_ref": "test-ref-0001"}
    first = await client.post(f"/staff/api/orders/{num}/collect", json=body)
    if first.status_code == 402:      # plan mein COD nahi — kuch jaanchne ko nahi
        return
    assert first.status_code == 200, first.text
    again = await client.post(f"/staff/api/orders/{num}/collect", json=body)
    assert again.status_code == 200 and again.json().get("duplicate") is True, again.text
    async with async_session_factory() as db:
        order = (await db.execute(select(Order).where(Order.order_number == num))).scalar_one()
        assert order.amount_paid == Decimal("100.00")
        n = (await db.execute(select(Payment).where(Payment.order_id == order.id))).scalars().all()
        assert len(n) == 1


async def test_order_resumed_from_hold_gets_its_task_back(two_shops, sent) -> None:
    from app.services import app_settings

    num = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    async with tenant_context.as_tenant(two_shops["a"]):
        async with async_session_factory() as db:
            await app_settings.set_value(db, "agent_auto_assign", True)
            order = (await db.execute(select(Order).where(Order.order_number == num))).scalar_one()
            await order_service.update_status(db, order, OrderStatus.ON_HOLD, changed_by="t")
            open_now = (await db.execute(
                select(Task).where(Task.order_id == order.id, Task.status == TASK_OPEN)
            )).scalars().all()
            assert open_now == []
            # IN_WASH par wapas — is status ka koi apna "stage task" nahi hota,
            # isliye pehle order kisi ki list mein aata hi nahi tha
            await order_service.update_status(db, order, OrderStatus.IN_WASH, changed_by="t")
            wash = (await db.execute(
                select(Task).where(Task.order_id == order.id, Task.status == TASK_OPEN, Task.kind == "wash")
            )).scalars().first()
            assert wash is not None, "resumed order must land on someone's list"


async def test_reassign_resets_reminders_and_order_owner(two_shops, sent) -> None:
    num = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    async with tenant_context.as_tenant(two_shops["a"]):
        async with async_session_factory() as db:
            order = (await db.execute(select(Order).where(Order.order_number == num))).scalar_one()
            boy = await db.get(Staff, two_shops["a_del"])
            task = await task_service.create_task(
                db, title="Deliver karo", staff=None, order=order, kind="delivery", notify=False,
            )
            task.ping_count = 3
            db.add(task)
            await db.commit()
            await task_service.apply_reassignment(db, task, boy)
            await db.commit()
            await db.refresh(task)
            await db.refresh(order)
            assert task.assigned_staff_id == boy.id and task.ping_count == 0
            assert order.assigned_delivery_id == boy.id
