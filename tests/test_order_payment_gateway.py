"""Razorpay customer order-payment webhook: signatures are tested separately."""
from decimal import Decimal
import uuid

from sqlalchemy import select

from app.database import async_session_factory
from app.models import Order, Payment, PaymentMethod
from app.services import order_service, tenant_context
from app.services.order_payment_gateway import handle_payment_link_paid


async def test_paid_payment_link_reconciles_once(sent) -> None:
    home = await tenant_context.get_home_tenant_id()
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            order = await order_service.create_order(
                db,
                customer_phone=f"+91{uuid.uuid4().int % 10**10:010d}",
                customer_name="Gateway Test",
                items=[{"type": "Shirt", "qty": 2, "rate": 50, "amount": 100}],
                total_amount=Decimal("100"),
                created_by="test",
            )
            order_id = order.id
            order_number = order.order_number

    link_id = "plink_test_" + uuid.uuid4().hex[:12]
    event = {
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": {
            "id": link_id,
            "status": "paid",
            "amount": 10000,
            "amount_paid": 10000,
            "notes": {"tenant_id": str(home), "order_id": str(order_id)},
        }}},
    }
    result = await handle_payment_link_paid(event)
    assert result["status"] == "recorded"
    replay = await handle_payment_link_paid(event)
    assert replay["status"] == "duplicate"

    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            updated = await db.get(Order, order_id)
            assert updated is not None
            assert updated.order_number == order_number
            assert updated.amount_paid == Decimal("100.00")
            assert updated.payment_status.name == "PAID"
            rows = (
                await db.execute(select(Payment).where(Payment.provider_reference == link_id))
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].method is PaymentMethod.UPI
