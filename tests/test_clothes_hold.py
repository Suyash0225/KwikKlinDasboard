"""Washerman kuch kapde rok sake ("ye abhi nahi hua") — partial delivery jaisa.

Roke hue kapde delivery mein nahi jaate, order tab tak DELIVERED nahi hota,
aur washerman "Hold done" karke unhe wapas delivery ke liye khol deta hai.
"""

from app.models import Order, OrderStatus
from app.services import delivery
from tests.test_staff_panel import (  # noqa: F401 (two_shops fixture yahin se aata hai)
    A_MGR_PHONE, A_PHONE, CUST_A, _login, _order_for, two_shops,
)


def _order(items) -> Order:
    return Order(order_number="KK-T", items=items, status=OrderStatus.IN_WASH)


def test_lines_show_held_and_deliverable() -> None:
    o = _order([
        {"type": "Shirt", "qty": 3, "held": 1, "hold_reason": "Stain not gone"},
        {"type": "Wash & Fold", "service": "Wash & Fold", "unit": "kg", "qty": 4,
         "pieces": [{"type": "Towel", "qty": 2, "held": 2}, {"type": "Jeans", "qty": 1}]},
    ])
    shirt, bag = delivery.lines(o)
    assert (shirt["held"], shirt["deliverable"], shirt["hold_reason"]) == (1, 2, "Stain not gone")
    assert bag["held"] == 2 and bag["pieces"][0]["deliverable"] == 0 and bag["pieces"][1]["deliverable"] == 1
    assert delivery.counts(o) == {"total": 6, "delivered": 0, "pending": 6, "held": 3}


def test_unitless_fractional_qty_is_a_kg_bag() -> None:
    # Purane WhatsApp bill unit nahi likhte the — 2.5 kapde nahi, 2.5 kg hai
    (row,) = delivery.lines(_order([{"type": "Wash & Fold", "qty": 2.5}]))
    assert row["kg"] and row["bag"] and row["weight"] == 2.5


def test_apply_hold_and_release() -> None:
    items = [{"type": "Shirt", "qty": 3}, {"type": "Saree", "unit": "kg", "qty": 2}]
    assert delivery._apply(items, [{"line": 0, "qty": 2}, {"line": 1, "qty": 1}], hold=True) == [
        "Shirt ×2", "Saree (bag)"]
    assert items[0]["held"] == 2 and items[1]["held_all"] is True
    try:
        delivery._apply(items, [{"line": 0, "qty": 2}], hold=True)
        raise AssertionError("only 1 shirt is free")
    except delivery.DeliveryError:
        pass
    assert delivery._apply(items, None, hold=False) == ["Shirt ×2", "Saree (bag)"]
    assert "held" not in items[0] and "held_all" not in items[1]


async def test_washer_holds_then_delivery_waits_for_the_held_clothes(client, two_shops, sent) -> None:
    num = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)   # 2 Kurta

    await _login(client, A_PHONE)
    assert (await client.post(f"/staff/api/orders/{num}/washing")).status_code == 200
    r = await client.post(f"/staff/api/orders/{num}/hold", json={
        "picks": [{"line": 0, "qty": 1}], "reason": "Stain not gone",
    })
    assert r.status_code == 200, r.text
    assert r.json()["clothes"]["held"] == 1
    # Bina wajah ke nahi
    assert (await client.post(f"/staff/api/orders/{num}/hold", json={
        "picks": [{"line": 0, "qty": 1}], "reason": " ",
    })).status_code in (409, 422)
    assert (await client.post(f"/staff/api/orders/{num}/ready")).status_code == 200

    # Delivery: sirf 1 de sakta hai, order DELIVERED nahi hota
    await _login(client, A_MGR_PHONE)
    r = await client.post(f"/staff/api/orders/{num}/deliver", json={"items": None})
    assert r.status_code == 200, r.text
    assert r.json()["delivered_now"] == 1 and r.json()["pending"] == 1
    assert r.json()["status"] == "OUT_FOR_DELIVERY"
    r = await client.post(f"/staff/api/orders/{num}/deliver", json={"items": None})
    assert r.status_code == 409 and "on hold" in r.json()["detail"]

    # Washerman ko order abhi bhi dikhta hai ("On hold") aur wo Hold done karta hai
    await _login(client, A_PHONE)
    stop = next(s for s in (await client.get("/staff/api/route")).json()["stops"] if s["number"] == num)
    assert stop["kind"] == "Hold" and stop["clothes"]["held"] == 1
    r = await client.post(f"/staff/api/orders/{num}/release", json={})
    assert r.status_code == 200 and not r.json()["clothes"].get("held"), r.text

    await _login(client, A_MGR_PHONE)
    r = await client.post(f"/staff/api/orders/{num}/deliver", json={"items": None})
    assert r.status_code == 200 and r.json()["status"] == "DELIVERED", r.text
