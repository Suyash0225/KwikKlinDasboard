"""AI Operations Playbook code-alignment tests."""

from datetime import date, timedelta

from app.database import async_session_factory
from app.models import Order, OrderStatus
from app.services.bill_agent import _apply_delay
from app.services.order_service import create_order
from sqlalchemy import select


PHONE = "+919999900099"


async def test_change_delivery_date_resolves_unique_customer_name(sent):
    from tests.conftest import purge_phones
    async with async_session_factory() as db:
        order = await create_order(
            db,
            customer_phone=PHONE,
            customer_name="Shikhar",
            items=[{"type": "shirt", "qty": 1}],
            created_by="test",
        )
        new_date = date.today() + timedelta(days=4)

        reply = await _apply_delay(
            db,
            "manager",
            {
                "order_number": "",
                "customer_name": "Shikhar",
                "new_date": new_date.isoformat(),
                "reason": "customer requested",
            },
        )

        fresh = (
            await db.execute(
                select(Order).where(Order.order_number == order.order_number)
            )
        ).scalar_one()
        assert fresh.expected_delivery == new_date
        assert order.order_number in reply
        assert new_date.strftime("%d %b %Y") in reply
    await purge_phones(PHONE)


async def test_change_delivery_date_does_not_guess_multiple_orders(sent):
    from tests.conftest import purge_phones
    async with async_session_factory() as db:
        await create_order(
            db,
            customer_phone=PHONE,
            customer_name="Shikhar",
            items=[{"type": "shirt", "qty": 1}],
            created_by="test",
        )
        await create_order(
            db,
            customer_phone="+919999900098",
            customer_name="Shikhar",
            items=[{"type": "pant", "qty": 1}],
            created_by="test",
        )

        reply = await _apply_delay(
            db,
            "manager",
            {
                "order_number": "",
                "customer_name": "Shikhar",
                "new_date": (date.today() + timedelta(days=4)).isoformat(),
                "reason": "",
            },
        )

        assert "active orders" in reply
        assert "Order number bata dijiye" in reply
    await purge_phones(PHONE)
    await purge_phones("+919999900098")
