"""Admin API + dashboard endpoint tests."""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory

PHONE = "+919999900066"
AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
ORDER_BODY = {
    "customer_phone": PHONE,
    "customer_name": "API Grahak",
    "items": [{"type": "saree", "qty": 2, "service": "dry_clean"}],
    "total_amount": "400.00",
}


@pytest.fixture(autouse=True)
async def _cleanup(sent):  # sent: block real WhatsApp calls from notifications
    yield
    async with async_session_factory() as s:
        await s.execute(
            sqltext(
                "DELETE FROM conversations WHERE customer_id IN "
                f"(SELECT id FROM customers WHERE phone = '{PHONE}')"
            )
        )
        await s.execute(
            sqltext(
                "DELETE FROM order_status_history WHERE order_id IN "
                "(SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id "
                f" WHERE c.phone = '{PHONE}')"
            )
        )
        await s.execute(
            sqltext(
                "DELETE FROM orders WHERE customer_id IN "
                f"(SELECT id FROM customers WHERE phone = '{PHONE}')"
            )
        )
        await s.execute(sqltext(f"DELETE FROM customers WHERE phone = '{PHONE}'"))
        await s.commit()


async def test_auth_required_everywhere(client) -> None:
    assert (await client.get("/orders")).status_code == 401
    assert (await client.post("/orders", json=ORDER_BODY)).status_code == 401
    assert (await client.get("/admin/api/dashboard")).status_code == 401
    bad = {"X-API-Key": "galat-key"}
    assert (await client.get("/orders", headers=bad)).status_code == 401


async def test_create_get_status_flow(client) -> None:
    r = await client.post("/orders", json=ORDER_BODY, headers=AUTH)
    assert r.status_code == 201, r.text
    number = r.json()["order_number"]
    assert r.json()["payment_status"] == "UNPAID"

    r = await client.get(f"/orders/{number}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["history"][0]["new_status"] == "RECEIVED"

    r = await client.post(f"/orders/{number}/status", json={"status": "in_wash"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "IN_WASH"

    # backward move -> 409 from the state machine
    r = await client.post(f"/orders/{number}/status", json={"status": "RECEIVED"}, headers=AUTH)
    assert r.status_code == 409

    r = await client.post(
        f"/orders/{number}/payment", json={"amount": "400.00", "method": "UPI"}, headers=AUTH
    )
    assert r.status_code == 200 and r.json()["payment_status"] == "PAID"


async def test_list_filters(client) -> None:
    r = await client.post("/orders", json=ORDER_BODY, headers=AUTH)
    number = r.json()["order_number"]

    r = await client.get("/orders", params={"active": True}, headers=AUTH)
    assert number in [o["order_number"] for o in r.json()]

    r = await client.get("/orders", params={"status": "received"}, headers=AUTH)
    assert number in [o["order_number"] for o in r.json()]

    assert (await client.get("/orders", params={"status": "nakli"}, headers=AUTH)).status_code == 400


async def test_dashboard_data_shape(client) -> None:
    await client.post("/orders", json=ORDER_BODY, headers=AUTH)
    r = await client.get("/admin/api/dashboard", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert d["counts"]["active_total"] >= 1
    assert any(o["customer"] == "API Grahak" for o in d["active_orders"])
    # notes (internal) must never appear in dashboard rows
    assert all("notes" not in o for o in d["active_orders"])


async def test_dashboard_page_serves(client) -> None:
    r = await client.get("/admin")
    assert r.status_code == 200
    assert "Laundry Pro" in r.text
    assert "New Bill" in r.text and "Expenses" in r.text  # CRM sections present


async def test_customers_endpoint(client) -> None:
    assert (await client.get("/admin/api/customers")).status_code == 401
    await client.post("/orders", json=ORDER_BODY, headers=AUTH)
    r = await client.get("/admin/api/customers", headers=AUTH)
    assert r.status_code == 200
    me = [c for c in r.json() if c["phone"] == PHONE]
    assert me and me[0]["total_orders"] >= 1 and me[0]["name"] == "API Grahak"
