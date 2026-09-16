"""⚡ Urgent kapde (BUG_007): bill banate waqt urgent + optional extra charge."""

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.services import urgent as urgent_svc
from app.services.work_orders import items_summary
from tests.conftest import purge_phones
from tests.test_expenses_and_rates import _cleanup_tenant_rows
from tests.test_staff_panel import A_DEL_PHONE, _login, two_shops  # noqa: F401

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


async def _order_row(number: str, phone: str) -> dict:
    # order number har dukaan ka apna hai — doosri dukaan mein wahi number
    # ho sakta hai, isliye grahak ke number se bhi baandho
    async with async_session_factory() as db:
        r = (await db.execute(sqltext(
            "SELECT o.items, o.priority, o.total_amount, o.expected_delivery FROM orders o"
            " JOIN customers c ON c.id = o.customer_id"
            " WHERE o.order_number = :n AND c.phone = :p"
        ), {"n": number, "p": phone})).mappings().one()
        return dict(r)


def test_default_charge_is_percent_or_flat_in_whole_rupees() -> None:
    assert urgent_svc.default_charge({"type": "percent", "value": 50}, Decimal("161")) == Decimal("81")
    assert urgent_svc.default_charge({"type": "flat", "value": 40}, Decimal("161")) == Decimal("40")
    assert urgent_svc.default_charge({"type": "percent", "value": 0}, Decimal("161")) == Decimal("0")


def test_urgent_line_is_not_counted_as_clothes() -> None:
    order = SimpleNamespace(items=[{"type": "Shirt", "qty": 2}, urgent_svc.line(Decimal("80"))])
    assert items_summary(order) == "2 x Shirt"


async def test_staff_urgent_bill_gets_default_charge_date_and_flag(client, two_shops) -> None:
    try:
        await _login(client, A_DEL_PHONE)
        me = (await client.get("/staff/api/me")).json()
        assert me["urgent"] == {"type": "percent", "value": 50.0, "days": 1}
        assert (await client.post("/staff/api/rates", json={
            "service": "Dry Clean", "garment": "Kurta", "unit": "pc", "rate": 80,
        })).status_code == 201

        # default: 2 x 80 = 160, +50% = 80
        r = await client.post("/staff/api/bills", json={
            "customer_phone": "9999900204", "urgent": True,
            "items": [{"service": "Dry Clean", "garment": "Kurta", "qty": 2}],
        })
        assert r.status_code == 201, r.text
        assert r.json()["total"] == 240
        row = await _order_row(r.json()["order_number"], "+919999900204")
        assert row["priority"] == "urgent"
        assert row["expected_delivery"] == date.today() + timedelta(days=1)
        charge = [i for i in row["items"] if i.get("kind") == "urgent_charge"]
        assert charge and charge[0]["amount"] == 80
        receipt = (await client.get(f"/staff/api/orders/{r.json()['order_number']}/receipt")).json()
        assert "Priority: ⚡ URGENT" in receipt["text"]
        assert "Urgent charge: +₹80" in receipt["text"] and "Subtotal: ₹160" in receipt["text"]
        assert "Priority" in receipt["print_text"] and "URGENT" in receipt["print_text"]

        # maaf: charge 0 -> total wahi, line nahi, par order urgent
        r = await client.post("/staff/api/bills", json={
            "customer_phone": "9999900204", "urgent": True, "urgent_charge": 0,
            "items": [{"service": "Dry Clean", "garment": "Kurta", "qty": 2}],
        })
        assert r.status_code == 201 and r.json()["total"] == 160
        row = await _order_row(r.json()["order_number"], "+919999900204")
        assert row["priority"] == "urgent"
        assert not [i for i in row["items"] if i.get("kind") == "urgent_charge"]

        # normal bill: kuch nahi badla
        r = await client.post("/staff/api/bills", json={
            "customer_phone": "9999900204",
            "items": [{"service": "Dry Clean", "garment": "Kurta", "qty": 1}],
        })
        row = await _order_row(r.json()["order_number"], "+919999900204")
        assert r.json()["total"] == 80 and row["priority"] == "normal"
    finally:
        client.cookies.clear()
        await _cleanup_tenant_rows(two_shops["a"])


async def test_dashboard_bill_can_be_urgent(client) -> None:
    phone = "+919999900093"
    try:
        r = await client.post("/orders", json={
            "customer_phone": phone, "total_amount": "150", "priority": "urgent",
            "items": [
                {"type": "Shirt", "service": "Dry Clean", "qty": 1, "rate": 100, "amount": 100, "unit": "pc"},
                {"type": "Urgent charge", "service": "Urgent", "qty": 1, "rate": 50, "amount": 50,
                 "unit": "pc", "kind": "urgent_charge"},
            ],
        }, headers=AUTH)
        assert r.status_code == 201, r.text
        row = await _order_row(r.json()["order_number"], phone)
        assert row["priority"] == "urgent"
        text = (await client.get(f"/orders/{r.json()['order_number']}/receipt", headers=AUTH)).json()["text"]
        assert "⚡ URGENT" in text and "Urgent charge: +₹50" in text
        assert "1 x ₹50" not in text   # urgent line kapde ki tarah nahi chhapti
    finally:
        await purge_phones(phone)
