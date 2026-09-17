"""Washerman ko kapde dikhte hain, paisa nahi — server hi rokta hai (UI nahi).

Delivery boy / manager ko total, due, collect, bill share sab pehle jaisa.
"""

from tests.test_staff_panel import (  # noqa: F401 (two_shops fixture yahin se aata hai)
    A_DEL_PHONE, A_PHONE, CUST_A, _login, _order_for, two_shops,
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
