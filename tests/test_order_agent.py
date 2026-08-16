"""Owner's Order Agent spec: pickup states, SLA, done command, role scope."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

import app.services.bill_agent as bill_module
from app.database import async_session_factory
from app.models import Order, OrderStatus
from app.services.bill_agent import handle_staff_message
from app.services.order_service import create_order, update_status
from tests.conftest import TEST_WASHER_NAME, TEST_WASHER_PHONE

PHONE = "+919999900555"
SUPERMAN = "+919336393612"  # seeded DELIVERY role


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    from tests.conftest import purge_phones

    await purge_phones(PHONE)


async def test_picked_up_sets_sla_and_notifies(sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=PHONE, customer_name="SLA Grahak",
            items=[{"type": "Blanket", "qty": 1}], created_by="test",
        )
        await update_status(db, order, OrderStatus.PICKUP_ASSIGNED, changed_by="agent")
        sent.clear()
        await update_status(db, order, OrderStatus.PICKED_UP, changed_by="Superman")
    # heavy item (blanket) -> 7 din from pickup day
    assert order.expected_delivery == date.today() + timedelta(days=7)
    assert any("pickup ho gaye" in (c["text"] or "") for c in sent)


async def test_done_command_by_delivery_boy(sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=PHONE, items=[{"type": "Shirt", "qty": 2}],
            created_by="test",
        )
        await update_status(db, order, OrderStatus.READY, changed_by="test")
        await update_status(db, order, OrderStatus.OUT_FOR_DELIVERY, changed_by="test")
        reply = await handle_staff_message(
            db, sender_phone=SUPERMAN, sender_label="Superman",
            text=f"done {order.order_number}",
        )
    assert "DELIVERED" in reply
    async with async_session_factory() as s:
        fresh = (
            await s.execute(select(Order).where(Order.order_number == order.order_number))
        ).scalar_one()
        assert fresh.status is OrderStatus.DELIVERED


async def test_washer_cannot_touch_delivery_status(monkeypatch, sent, test_washer) -> None:
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=PHONE, items=[{"type": "Shirt", "qty": 1}],
            created_by="test",
        )

    async def fake_ask_json(**kw):
        return {
            "action": "status_update", "customer_name": "", "customer_phone": "",
            "items": [], "advance": 0, "expected_delivery": "",
            "order_number": order.order_number, "new_date": "", "reason": "",
            "new_status": "OUT_FOR_DELIVERY", "relay_to": "", "relay_message": "",
            "priority": "NONE", "staff_name": "", "note": "", "amount": 0,
            "method": "NONE", "done_refs": [], "pending_refs": [], "problem": "",
        }

    monkeypatch.setattr(bill_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=TEST_WASHER_PHONE, sender_label=TEST_WASHER_NAME,
            text=f"{order.order_number} delivery pe nikal gaya",
        )
    assert "role ka kaam nahi" in reply
    async with async_session_factory() as s:
        fresh = (
            await s.execute(select(Order).where(Order.order_number == order.order_number))
        ).scalar_one()
        assert fresh.status is OrderStatus.RECEIVED  # unchanged
