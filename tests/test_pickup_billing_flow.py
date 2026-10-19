"""Regression tests for pickup-linked billing in the existing staff New Bill UI."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text as sqltext

from app.database import async_session_factory
from app.models import Order, OrderStatus, Staff, Task
from app.services import order_service, tasks as task_service, tenant_context
from tests.test_staff_panel import A_DEL_PHONE, CUST_A, _login, _panel_rate, two_shops


async def test_pickup_bill_reuses_old_order_and_is_visible_in_recent_bills(
    client, two_shops, sent
):
    await _panel_rate(
        two_shops["a"], service="PickupRegressionSvc", garment="Shirt", rate="50"
    )
    try:
        token = tenant_context.current_tenant_id.set(two_shops["a"])
        try:
            async with async_session_factory() as db:
                order = await order_service.create_order(
                    db,
                    customer_phone=CUST_A,
                    customer_name="Pickup Regression",
                    items=[{"type": "Shirt", "qty": 1}],
                    needs_pickup=True,
                    created_by="test",
                )
                # Simulate a pickup request created weeks ago. The bill is
                # new today, so it must still appear in the default recent list.
                order.created_at = datetime.now(timezone.utc) - timedelta(days=30)
                task = (
                    await db.execute(
                        select(Task).where(
                            Task.order_id == order.id,
                            Task.kind == "pickup",
                            Task.status == "OPEN",
                        )
                    )
                ).scalar_one_or_none()
                delivery = (
                    await db.execute(
                        select(Staff).where(
                            Staff.phone == A_DEL_PHONE,
                            Staff.tenant_id == two_shops["a"],
                        )
                    )
                ).scalar_one()
                if task is None:
                    task = await task_service.create_task(
                        db,
                        title="Pickup: Pickup Regression",
                        staff=delivery,
                        order=order,
                        notify=False,
                        kind="pickup",
                    )
                else:
                    task.assigned_staff_id = delivery.id
                    order.assigned_delivery_id = delivery.id
                await db.commit()
                code = task.code
                original_order_id = order.id
        finally:
            tenant_context.current_tenant_id.reset(token)

        await _login(client, A_DEL_PHONE)
        context = await client.get(f"/staff/api/tasks/{code}/bill-context")
        assert context.status_code == 200, context.text
        ctx = context.json()

        payload = {
            "pickup_task_code": code,
            "customer_ref": ctx["customer_ref"],
            "customer_phone": "",
            "customer_name": ctx["customer_name"],
            "items": [
                {
                    "service": "PickupRegressionSvc",
                    "garment": "Shirt",
                    "qty": 2,
                }
            ],
            "advance": 20,
        }
        # Invalid bill input must not save a partial order or close the pickup.
        invalid_payload = {**payload, "items": [{
            "service": "MissingService", "garment": "Shirt", "qty": 2
        }]}
        invalid = await client.post("/staff/api/bills", json=invalid_payload)
        assert invalid.status_code == 400, invalid.text
        async with async_session_factory() as db:
            unchanged = (
                await db.execute(select(Order).where(Order.id == original_order_id))
            ).scalar_one()
            still_open = (
                await db.execute(select(Task).where(Task.code == code))
            ).scalar_one()
            assert unchanged.total_amount is None
            assert still_open.status == "OPEN"

        r = await client.post("/staff/api/bills", json=payload)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["pickup_completed"] is True
        assert body["total"] == 100.0

        # The pickup order is updated in place, not duplicated.
        async with async_session_factory() as db:
            order = (
                await db.execute(select(Order).where(Order.id == original_order_id))
            ).scalar_one()
            assert order.order_number == body["order_number"]
            assert order.status is OrderStatus.PICKED_UP
            assert float(order.total_amount) == 100.0
            assert float(order.amount_paid) == 20.0
            task = (
                await db.execute(select(Task).where(Task.code == code))
            ).scalar_one()
            assert task.status == "DONE"
            wash = (
                await db.execute(
                    select(Task).where(
                        Task.order_id == order.id,
                        Task.kind == "wash",
                        Task.status == "OPEN",
                    )
                )
            ).scalar_one_or_none()
            assert wash is not None

        # A recent pickup bill should appear even when the pickup request
        # itself was created outside the default 14-day order window.
        bills = await client.get("/staff/api/bills")
        assert bills.status_code == 200, bills.text
        assert body["order_number"] in [
            item["number"] for item in bills.json()["bills"]
        ]

        # Duplicate submits cannot charge a second advance or complete twice.
        duplicate = await client.post("/staff/api/bills", json=payload)
        assert duplicate.status_code == 409, duplicate.text
    finally:
        async with async_session_factory() as db:
            await db.execute(
                sqltext(
                    "DELETE FROM rate_card WHERE tenant_id = :t "
                    "AND service = 'PickupRegressionSvc'"
                ),
                {"t": str(two_shops["a"])},
            )
            await db.commit()
