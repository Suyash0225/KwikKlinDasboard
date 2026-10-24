"""New bill (owner dashboard): sabse zyada bikne wale items upar, aur chune
hue grahak ke aam items. Dono purane bills se — `orders.items` hi sach hai.

Staff panel wala hissa test_staff_panel.py mein hai (wahan do dukaanein,
role aur tenant ke pehre ek saath)."""
from decimal import Decimal

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory

PHONE = "+919999900311"
OTHER = "+919999900312"
SVC = "SuggestSvc"
AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


@pytest.fixture(autouse=True)
async def _cleanup(sent):
    yield
    from tests.conftest import purge_phones

    await purge_phones(PHONE, OTHER)
    async with async_session_factory() as s:
        await s.execute(sqltext("DELETE FROM rate_card WHERE service = :s"), {"s": SVC})
        await s.commit()


def _line(garment: str, qty: float, **extra) -> dict:
    return {"type": garment, "service": SVC, "qty": qty, "rate": 10, "amount": 10 * qty, "unit": "pc", **extra}


async def _bill(client, phone: str, items: list[dict]) -> str:
    total = sum(Decimal(str(i["amount"])) for i in items)
    r = await client.post("/orders", headers=AUTH, json={
        "customer_phone": phone, "customer_name": "Suggest Grahak",
        "items": items, "total_amount": str(total),
    })
    assert r.status_code == 201, r.text
    return r.json()["order_number"]


async def _setup(client) -> None:
    for g in ("Shirt", "Pant", "Kurta"):
        r = await client.post("/admin/api/rates", headers=AUTH,
                              json={"service": SVC, "garment": g, "unit": "pc", "rate": "10"})
        assert r.status_code in (200, 201), r.text
    # purane se naya: Pant+Shirt, Shirt, Shirt+Kurta (+ urgent fee line)
    await _bill(client, PHONE, [_line("Pant", 1), _line("Shirt", 2)])
    await _bill(client, PHONE, [_line("Shirt", 3)])
    await _bill(client, PHONE, [_line("Shirt", 1), _line("Kurta", 5),
                                {"type": "Urgent charge", "service": "Urgent", "qty": 1, "rate": 30,
                                 "amount": 30, "unit": "pc", "kind": "urgent_charge"}])
    # cancelled bill kisi ginti mein nahi
    cancelled = await _bill(client, PHONE, [_line("Pant", 9)])
    async with async_session_factory() as s:
        await s.execute(sqltext("UPDATE orders SET status = 'CANCELLED' WHERE order_number = :n"), {"n": cancelled})
        await s.commit()


async def test_rates_carry_shop_wide_uses(client) -> None:
    await _setup(client)
    await _bill(client, OTHER, [_line("Kurta", 1)])          # doosra grahak bhi dukaan ki ginti mein
    rates = {r["garment"]: r for r in (await client.get("/admin/api/rates", headers=AUTH)).json()
             if r["service"] == SVC}
    # bills gine jaate hain, pieces nahi: Shirt 3 bill, Kurta 2, Pant 1 (cancelled nahi)
    assert (rates["Shirt"]["uses"], rates["Kurta"]["uses"], rates["Pant"]["uses"]) == (3, 2, 1)


async def test_usual_items_for_a_customer(client) -> None:
    await _setup(client)
    r = await client.get(f"/admin/api/customers/usual-items?phone={PHONE}", headers=AUTH)
    assert r.status_code == 200
    mine = [u for u in r.json()["items"] if u["service"] == SVC]
    # Shirt 3 baar (pichhli qty 1); Kurta aur Pant ek-ek — Kurta naya hai to pehle
    assert [(u["garment"], u["times"], u["qty"]) for u in mine] == [
        ("Shirt", 3, 1), ("Kurta", 1, 5), ("Pant", 1, 1)]
    assert not [u for u in r.json()["items"] if u["service"] == "Urgent"]   # fee line sujhaav nahi


async def test_inactive_rate_is_never_suggested(client) -> None:
    await _setup(client)
    rid = next(r["id"] for r in (await client.get("/admin/api/rates", headers=AUTH)).json()
               if r["service"] == SVC and r["garment"] == "Shirt")
    assert (await client.put(f"/admin/api/rates/{rid}", headers=AUTH, json={"is_active": False})).status_code == 200
    mine = [u["garment"] for u in
            (await client.get(f"/admin/api/customers/usual-items?phone={PHONE}", headers=AUTH)).json()["items"]
            if u["service"] == SVC]
    assert "Shirt" not in mine and mine == ["Kurta", "Pant"]


async def test_unknown_customer_and_auth(client) -> None:
    r = await client.get("/admin/api/customers/usual-items?phone=+919999900399", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"items": []}
    assert (await client.get(f"/admin/api/customers/usual-items?phone={PHONE}")).status_code == 401
