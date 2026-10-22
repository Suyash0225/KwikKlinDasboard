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


async def test_dashboard_pickup_task_without_order_opens_prefilled_bill(
    client, two_shops, sent
):
    """Owner ne dashboard se pickup diya — sirf grahak, order nahi. Delivery
    wala task se seedha bill banata hai: grahak bhara hua, naya order bane,
    task band aur order PICKED_UP."""
    from app.models import Customer
    from app.utils.phone import normalize_phone

    await _panel_rate(two_shops["a"], service="PickupNoOrderSvc", garment="Shirt", rate="40")
    try:
        token = tenant_context.current_tenant_id.set(two_shops["a"])
        try:
            async with async_session_factory() as db:
                cust = (
                    await db.execute(select(Customer).where(Customer.phone == normalize_phone(CUST_A)))
                ).scalar_one_or_none()
                if cust is None:
                    cust = Customer(phone=normalize_phone(CUST_A), name="Pickup Seedha")
                    db.add(cust)
                    await db.commit()
                delivery = (
                    await db.execute(
                        select(Staff).where(Staff.phone == A_DEL_PHONE, Staff.tenant_id == two_shops["a"])
                    )
                ).scalar_one()
                task = await task_service.create_task(
                    db, title="Sharma ji ke ghar se kapde lao", staff=delivery,
                    notify=False, kind="pickup", customer=cust,
                )
                code, cust_id = task.code, cust.id
        finally:
            tenant_context.current_tenant_id.reset(token)

        await _login(client, A_DEL_PHONE)
        card = next(t for t in (await client.get("/staff/api/tasks?tab=mine")).json()["tasks"] if t["code"] == code)
        assert card["kind"] == "pickup" and "order" not in card and card["customer"]["name"]

        ctx = await client.get(f"/staff/api/tasks/{code}/bill-context")
        assert ctx.status_code == 200, ctx.text
        ctx = ctx.json()
        assert ctx["order_number"] is None and ctx["customer_ref"] == str(cust_id)

        payload = {
            "pickup_task_code": code, "customer_ref": ctx["customer_ref"], "customer_phone": "",
            "customer_name": ctx["customer_name"],
            "items": [{"service": "PickupNoOrderSvc", "garment": "Shirt", "qty": 3}],
        }
        r = await client.post("/staff/api/bills", json=payload)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["pickup_completed"] is True and body["total"] == 120.0

        async with async_session_factory() as db:
            order = (await db.execute(select(Order).where(Order.order_number == body["order_number"]))).scalar_one()
            assert order.status is OrderStatus.PICKED_UP and order.customer_id == cust_id
            task = (await db.execute(select(Task).where(Task.code == code))).scalar_one()
            assert task.status == "DONE" and task.order_id == order.id

        assert (await client.post("/staff/api/bills", json=payload)).status_code == 409
    finally:
        async with async_session_factory() as db:
            await db.execute(
                sqltext("DELETE FROM rate_card WHERE tenant_id = :t AND service = 'PickupNoOrderSvc'"),
                {"t": str(two_shops["a"])},
            )
            await db.commit()


async def test_self_order_pickup_bill_prefills_items_and_applies_coupon(client, two_shops, sent):
    """/billcreation ka order: boy ke bill mein grahak ke kapde + coupon pehle
    se, aur coupon asli bill par lagta hai (pehle is raaste par chhoot jaata tha)."""
    from decimal import Decimal

    from app.models import Coupon
    from app.services import self_order

    await _panel_rate(two_shops["a"], service="SelfOrderPickupSvc", garment="Shirt", rate="50")
    token = tenant_context.current_tenant_id.set(two_shops["a"])
    try:
        async with async_session_factory() as db:
            db.add(Coupon(code="ZZPICK10", discount_type="percent", value=Decimal("10")))
            await db.commit()
            items, est = await self_order.price_items(db, [{"service": "SelfOrderPickupSvc", "garment": "Shirt", "qty": 2}])
            order, _ = await self_order.place_order(
                db, customer_phone=CUST_A, name="Self Order", address="1 Test Road", items=items,
                estimate=est, pickup_day=self_order._today(), via="test", coupon="ZZPICK10", discount=Decimal("10"),
            )
            delivery = (await db.execute(select(Staff).where(
                Staff.phone == A_DEL_PHONE, Staff.tenant_id == two_shops["a"]))).scalar_one()
            task = (await db.execute(select(Task).where(
                Task.order_id == order.id, Task.kind == "pickup", Task.status == "OPEN"))).scalar_one_or_none()
            if task is None:
                task = await task_service.create_task(
                    db, title="Pickup: Self Order", staff=delivery, order=order, notify=False, kind="pickup")
            else:
                task.assigned_staff_id = delivery.id
            await db.commit()
            code, order_id = task.code, order.id
    finally:
        tenant_context.current_tenant_id.reset(token)
    try:
        await _login(client, A_DEL_PHONE)
        ctx = (await client.get(f"/staff/api/tasks/{code}/bill-context")).json()
        assert ctx["items"] == [{"service": "SelfOrderPickupSvc", "garment": "Shirt", "qty": 2.0}]
        assert ctx["coupon_code"] == "ZZPICK10"

        r = await client.post("/staff/api/bills", json={
            "pickup_task_code": code, "customer_ref": ctx["customer_ref"], "customer_phone": "",
            "customer_name": ctx["customer_name"], "coupon_code": ctx["coupon_code"],
            "items": [{"service": "SelfOrderPickupSvc", "garment": "Shirt", "qty": 3}],
        })
        assert r.status_code == 201, r.text
        assert r.json()["total"] == 135.0                         # 150 − 10%
        async with async_session_factory() as db:
            used = (await db.execute(sqltext(
                "SELECT count(*) FROM coupon_redemptions WHERE order_id = :o"), {"o": str(order_id)})).scalar_one()
        assert used == 1
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM coupon_redemptions WHERE coupon_code = 'ZZPICK10'"))
            await db.execute(sqltext("DELETE FROM coupons WHERE code = 'ZZPICK10'"))
            await db.execute(sqltext(
                "DELETE FROM rate_card WHERE tenant_id = :t AND service = 'SelfOrderPickupSvc'"),
                {"t": str(two_shops["a"])})
            await db.commit()
