"""AI Operations Playbook code-alignment tests."""

from datetime import date, timedelta
from decimal import Decimal
import uuid

from sqlalchemy import select, text as sqltext

from app.database import async_session_factory
from app.models import Customer, Order
from app.services.bill_agent import _apply_delay
from app.services import tenant_context


PHONE = "+919999900099"
PHONE_2 = "+919999900098"


async def _seed_order(db, phone: str, name: str, item_type: str) -> Order:
    """Seed an order using only columns owned by the current application schema."""
    tenant_id = await tenant_context.get_home_tenant_id()
    customer = Customer(
        phone=phone,
        name=name,
        tenant_id=tenant_id,
    )
    db.add(customer)
    await db.flush()

    order_id = uuid.uuid4()
    order_number = f"KK-TEST-{uuid.uuid4().hex[:10].upper()}"
    await db.execute(
        sqltext(
            """
            INSERT INTO orders (
                id, order_number, customer_id, status, items,
                amount_paid, payment_status, priority, tenant_id
            )
            VALUES (
                :id, :order_number, :customer_id, 'RECEIVED',
                CAST(:items AS jsonb), :amount_paid, 'UNPAID',
                'normal', :tenant_id
            )
            """
        ),
        {
            "id": str(order_id),
            "order_number": order_number,
            "customer_id": str(customer.id),
            "items": f'[{{"type":"{item_type}","qty":1}}]',
            "amount_paid": Decimal("0"),
            "tenant_id": str(tenant_id),
        },
    )
    await db.commit()
    return (
        await db.execute(select(Order).where(Order.id == order_id))
    ).scalar_one()


async def test_change_delivery_date_resolves_unique_customer_name(sent):
    from tests.conftest import purge_phones

    async with async_session_factory() as db:
        order = await _seed_order(db, PHONE, "Shikhar", "shirt")
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
        await _seed_order(db, PHONE, "Shikhar", "shirt")
        await _seed_order(db, PHONE_2, "Shikhar", "pant")

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
    await purge_phones(PHONE_2)
