"""Partial delivery (BUG_008) + order aage badhana (picked up / ready)."""

from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models import OrderStatus
from app.services import delivery
from app.services.order_service import create_order, get_order, update_status
from tests.conftest import purge_phones
from tests.test_expenses_and_rates import _cleanup_tenant_rows
from tests.test_staff_panel import A_DEL_PHONE, A_PHONE, _login, two_shops  # noqa: F401

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
PHONE = "+919999900096"


def test_lines_count_pieces_bags_and_skip_money_lines() -> None:
    order = SimpleNamespace(items=[
        {"type": "Shirt", "qty": 12, "delivered": 8},
        {"type": "Premium Wash", "unit": "kg", "qty": 4.5,
         "pieces": [{"type": "Towel", "qty": 5, "delivered": 2}, {"type": "Pant", "qty": 3}]},
        {"type": "Wash & Fold (kg)", "unit": "kg", "qty": 2},
        {"type": "Urgent charge", "qty": 1, "amount": 50, "kind": "urgent_charge"},
    ])
    ls = delivery.lines(order)
    assert [l["name"] for l in ls] == ["Shirt", "Premium Wash", "Wash & Fold (kg)"]
    assert ls[0]["pending"] == 4
    assert ls[1]["qty"] == 8 and ls[1]["delivered"] == 2 and ls[1]["pieces"][1]["pending"] == 3
    assert ls[2]["bag"] and ls[2]["pending"] == 1
    assert delivery.counts(order) == {"total": 21, "delivered": 10, "pending": 11}


async def _ready_order() -> str:
    async with async_session_factory() as db:
        o = await create_order(
            db, customer_phone=PHONE, created_by="test", total_amount=Decimal("600"),
            items=[{"type": "Shirt", "qty": 12, "unit": "pc"},
                   {"type": "Premium Wash", "unit": "kg", "qty": 3, "pieces": [{"type": "Towel", "qty": 4}]}],
        )
        await update_status(db, o, OrderStatus.READY, changed_by="test")
        return o.order_number


async def test_dashboard_partial_then_rest(client, sent) -> None:
    try:
        number = await _ready_order()
        d = (await client.get(f"/orders/{number}", headers=AUTH)).json()
        assert d["clothes"] == {"total": 16, "delivered": 0, "pending": 16}

        # 8 of 12 shirts + 1 towel
        r = await client.post(f"/orders/{number}/deliver", headers=AUTH, json={"items": [
            {"line": 0, "qty": 8}, {"line": 1, "piece": 0, "qty": 1},
        ]})
        assert r.status_code == 200, r.text
        assert r.json() == {"order_number": number, "delivered_now": 9, "pending": 7, "status": "OUT_FOR_DELIVERY"}

        # zyada dena mana
        bad = await client.post(f"/orders/{number}/deliver", headers=AUTH, json={"items": [{"line": 0, "qty": 5}]})
        assert bad.status_code == 409 and "Only 4" in bad.json()["detail"]
        # paise ki line / galat line mana
        assert (await client.post(f"/orders/{number}/deliver", headers=AUTH,
                                  json={"items": [{"line": 9, "qty": 1}]})).status_code == 409

        # baaki sab
        r = await client.post(f"/orders/{number}/deliver", headers=AUTH, json={"items": None})
        assert r.json()["pending"] == 0 and r.json()["status"] == "DELIVERED"
        async with async_session_factory() as db:
            o = await get_order(db, number)
            assert o.items[0]["delivered"] == 12 and o.items[1]["pieces"][0]["delivered"] == 4
        # delivered order dobara nahi
        assert (await client.post(f"/orders/{number}/deliver", headers=AUTH, json={})).status_code == 409
    finally:
        await purge_phones(PHONE)


async def test_staff_delivery_boy_partial_and_detail(client, two_shops, sent) -> None:
    try:
        await _login(client, A_DEL_PHONE)
        assert (await client.post("/staff/api/rates", json={
            "service": "Dry Clean", "garment": "Shirt", "unit": "pc", "rate": 50,
        })).status_code == 201
        r = await client.post("/staff/api/bills", json={
            "customer_phone": "9999900204",
            "items": [{"service": "Dry Clean", "garment": "Shirt", "qty": 12}],
        })
        number = r.json()["order_number"]
        async with async_session_factory() as db:
            await db.execute(sqltext(
                "UPDATE orders SET status = 'READY' WHERE order_number = :n AND tenant_id = :t"
            ), {"n": number, "t": str(two_shops["a"])})
            await db.commit()

        r = await client.post(f"/staff/api/orders/{number}/deliver", json={"items": [{"line": 0, "qty": 8}]})
        assert r.status_code == 200, r.text
        assert r.json()["pending"] == 4 and r.json()["status"] == "OUT_FOR_DELIVERY"
        detail = (await client.get(f"/staff/api/orders/{number}")).json()
        assert detail["clothes"] == {"total": 12, "delivered": 8, "pending": 4}
        assert detail["lines"][0]["name"] == "Shirt" and detail["lines"][0]["pending"] == 4

        # washerman delivery nahi kar sakta
        client.cookies.clear()
        await _login(client, A_PHONE)
        assert (await client.post(f"/staff/api/orders/{number}/deliver", json={})).status_code == 403
    finally:
        client.cookies.clear()
        async with async_session_factory() as db:
            await db.execute(sqltext(
                "DELETE FROM order_status_history WHERE order_id IN (SELECT id FROM orders WHERE tenant_id = :t)"
            ), {"t": str(two_shops["a"])})
            await db.commit()
        await _cleanup_tenant_rows(two_shops["a"])


async def test_picked_up_moves_the_order(client, sent) -> None:
    try:
        async with async_session_factory() as db:
            o = await create_order(db, customer_phone=PHONE, created_by="test", needs_pickup=True,
                                   items=[{"type": "Shirt", "qty": 2}])
            assert o.status is OrderStatus.PICKUP_ASSIGNED
            out = await delivery.picked_up(db, o, by="test")
            assert out["status"] == "PICKED_UP"
            try:
                await delivery.picked_up(db, o, by="test")
                raise AssertionError("second pickup should fail")
            except delivery.DeliveryError:
                pass
    finally:
        await purge_phones(PHONE)
