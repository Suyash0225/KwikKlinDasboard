"""Audit batch 1 — task/delivery/paisa/WhatsApp ke pakke bug."""

import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models import Order, OrderStatus
from app.services import delivery, tenant_context
from app.services.order_service import InvalidTransitionError, update_status
from tests.test_staff_panel import (  # noqa: F401 (two_shops fixture yahin se aata hai)
    A_DEL_PHONE, A_PHONE, CUST_A, _login, _order_for, two_shops,
)
from tests.test_staff_tools import _set_status


def test_extra_and_due_charge_lines_are_not_clothes() -> None:
    o = Order(order_number="KK-T", status=OrderStatus.READY, items=[
        {"type": "Shirt", "qty": 2},
        {"type": "Extra", "kind": "extra_charge", "amount": 20},
        {"type": "Purana due", "kind": "due_charge", "amount": 50},
    ])
    assert delivery.counts(o) == {"total": 2, "delivered": 0, "pending": 2}


async def test_delivered_refuses_held_clothes_and_marks_the_rest(two_shops, sent) -> None:
    num = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)   # 2 Kurta
    await _set_status(two_shops["a"], num, "OUT_FOR_DELIVERY")
    async with tenant_context.as_tenant(two_shops["a"]):
        async with async_session_factory() as db:
            order = (await db.execute(select(Order).where(Order.order_number == num))).scalar_one()
            await delivery.hold(db, order, [{"line": 0, "qty": 1}], reason="Stain not gone", by="t")
            with pytest.raises(InvalidTransitionError):
                await update_status(db, order, OrderStatus.DELIVERED, changed_by="t")
            await delivery.release(db, order, None, by="t")
            await update_status(db, order, OrderStatus.DELIVERED, changed_by="t")
            # Seedha DELIVERED: line-wise hisaab bhi "sab diye" kehta hai
            assert delivery.counts(order)["pending"] == 0


async def test_washer_done_command_moves_to_the_next_stage_not_ready(two_shops, sent) -> None:
    from app.services import bill_agent

    num = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    await _set_status(two_shops["a"], num, "IN_WASH")
    async with tenant_context.as_tenant(two_shops["a"]):
        async with async_session_factory() as db:
            await bill_agent._apply_done(db, A_PHONE, "Awash", num)
            order = (await db.execute(select(Order).where(Order.order_number == num))).scalar_one()
            assert order.status is OrderStatus.IN_IRON


async def test_collect_refuses_a_cancelled_order(client, two_shops, sent) -> None:
    """Cancel hue order par paisa jama nahi (pehle koi bhi order chalta tha)."""
    num = await _order_for(two_shops["a"], two_shops["a_del"], CUST_A, delivery=True, total=300)
    await _set_status(two_shops["a"], num, "CANCELLED")
    await _login(client, A_DEL_PHONE)
    r = await client.post(f"/staff/api/orders/{num}/collect", json={"amount": 100, "method": "cash"})
    assert r.status_code in (402, 409), r.text


async def test_status_routing_leaves_new_pickup_requests_to_the_ai() -> None:
    from app.routers.webhook import _NEW_WORK_RE, ORDER_STATUS_QUERY_RE

    for t in ("kal kapde pickup kar lo, delivery 2 din mein",
              "kapde dhulwane hai, ready kitne din me hoga"):
        assert ORDER_STATUS_QUERY_RE.search(t) and _NEW_WORK_RE.search(t)
    assert not _NEW_WORK_RE.search("mera order kahan hai")
