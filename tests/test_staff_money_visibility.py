"""Washerman ko kapde dikhte hain, paisa nahi — server hi rokta hai (UI nahi).

Delivery boy / manager ko total, due, collect, bill share sab pehle jaisa.
"""

from sqlalchemy import select

from app.database import async_session_factory
from tests.test_staff_panel import (  # noqa: F401 (two_shops fixture yahin se aata hai)
    A_DEL_PHONE, A_MGR_PHONE, A_PHONE, CUST_A, _login, _order_for, two_shops,
)

MONEY = ("/receipt", "/dues")


async def test_washer_gets_no_money_fields_and_no_money_endpoints(client, two_shops, sent) -> None:
    num = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A, total=300)
    await _login(client, A_PHONE)

    me = (await client.get("/staff/api/me")).json()
    assert me["can_money"] is False and me["can_bill"] is False

    stops = (await client.get("/staff/api/route")).json()["stops"]
    mine = next(s for s in stops if s["number"] == num)
    assert "due" not in mine and "total" not in mine

    d = (await client.get(f"/staff/api/orders/{num}")).json()
    assert d["number"] == num and "clothes" in d
    for key in ("total", "paid", "due", "urgent_charge", "can_collect"):
        assert key not in d, key

    for path in MONEY:
        r = await client.get(f"/staff/api/orders/{num}{path}")
        assert r.status_code == 403, (path, r.status_code)
    r = await client.post(f"/staff/api/orders/{num}/collect", json={"amount": 100, "method": "CASH"})
    assert r.status_code == 403
    r = await client.get(f"/staff/api/orders/{num}/message?kind=service_thanks")
    assert r.status_code == 403
    today = (await client.get("/staff/api/today")).json()
    assert today["can_collect"] is False


async def test_delivery_boy_still_sees_money(client, two_shops, sent) -> None:
    num = await _order_for(two_shops["a"], two_shops["a_del"], CUST_A, delivery=True, total=300)
    await _login(client, A_DEL_PHONE)
    assert (await client.get("/staff/api/me")).json()["can_money"] is True
    d = (await client.get(f"/staff/api/orders/{num}")).json()
    assert d["total"] == 300.0 and d["due"] == 300.0
    assert (await client.get(f"/staff/api/orders/{num}/receipt")).status_code == 200
    assert (await client.get(f"/staff/api/orders/{num}/dues")).status_code == 200


async def test_washer_cannot_reach_the_customer_but_can_move_the_wash(client, two_shops, sent) -> None:
    """Washerman ko grahak ka number/pata nahi (call bhi nahi) — use sirf
    kapde, status (Start wash -> Ready) aur photo chahiye."""
    num = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A, total=300)
    await _login(client, A_PHONE)
    me = (await client.get("/staff/api/me")).json()
    assert me["can_contact"] is False

    assert (await client.get(f"/staff/api/orders/{num}/call")).status_code == 403
    d = (await client.get(f"/staff/api/orders/{num}")).json()
    assert "phone_masked" not in d
    stop = next(s for s in (await client.get("/staff/api/route")).json()["stops"] if s["number"] == num)
    assert "phone_masked" not in stop and stop["address"] == ""

    r = await client.post(f"/staff/api/orders/{num}/washing")
    assert r.status_code == 200 and r.json()["status"] == "IN_WASH", r.text
    assert (await client.post(f"/staff/api/orders/{num}/washing")).status_code == 409   # dobara nahi
    r = await client.post(f"/staff/api/orders/{num}/ready")
    assert r.status_code == 200 and r.json()["status"] == "READY", r.text

    # Ready ke baad kaam list se hat jaata hai, par "Done" mein rehta hai (7 din)
    todo = [s["number"] for s in (await client.get("/staff/api/route")).json()["stops"]]
    done = (await client.get("/staff/api/route?tab=done")).json()["stops"]
    assert num not in todo
    mine = next(s for s in done if s["number"] == num)
    assert mine["kind"] == "Done" and mine["status"] == "READY" and mine["done_at"]
    assert "due" not in mine and "phone_masked" not in mine

    # Delivery wale ko number/pata pehle jaisa
    await _login(client, A_DEL_PHONE)
    assert (await client.get("/staff/api/me")).json()["can_contact"] is True


async def test_boot_returns_everything_the_panel_needs_in_one_call(client, two_shops, sent) -> None:
    """Panel khulte hi ek request: me + route + tasks + today + notifications."""
    num = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A, total=300)
    await _login(client, A_PHONE)
    b = (await client.get("/staff/api/boot")).json()
    assert b["me"]["role"] == "WASHER" and b["me"]["can_money"] is False
    assert num in [s["number"] for s in b["route"]["stops"]]
    assert "tasks" in b["tasks"] and "pending" in b["today"] and "unread" in b["notifications"]
    assert "due" not in next(s for s in b["route"]["stops"] if s["number"] == num)
    await client.post("/staff/api/logout")
    assert (await client.get("/staff/api/boot")).status_code == 401


async def test_manager_sees_ready_orders_to_deliver_washer_does_not(client, two_shops, sent) -> None:
    """Ready order manager/owner ke panel mein 'Delivery' ban kar aaye (partial
    delivery yahin se hoti hai); washerman ki kataar mein nahi."""
    from app.models import Order, OrderStatus
    from app.services import order_service, tenant_context

    num = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A, total=200)
    async with tenant_context.as_tenant(two_shops["a"]):
        async with async_session_factory() as db:
            o = (await db.execute(select(Order).where(Order.order_number == num))).scalar_one()
            await order_service.update_status(db, o, OrderStatus.READY, changed_by="test", notify=False)

    await _login(client, A_MGR_PHONE)
    stop = next((s for s in (await client.get("/staff/api/route")).json()["stops"] if s["number"] == num), None)
    assert stop is not None and stop["kind"] == "Delivery" and stop["status"] == "READY"
    assert stop["due"] == 200.0 and stop["clothes"]["pending"] > 0

    await _login(client, A_PHONE)
    assert num not in [s["number"] for s in (await client.get("/staff/api/route")).json()["stops"]]
