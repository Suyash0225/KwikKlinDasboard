"""Billing upgrade tests: discount/GST/advance, expenses, reports, CSV export."""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory

PHONE = "+919999900099"
AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


@pytest.fixture(autouse=True)
async def _cleanup(sent):
    yield
    from tests.conftest import purge_phones

    await purge_phones(PHONE)
    async with async_session_factory() as s:
        await s.execute(sqltext("DELETE FROM expenses WHERE description = 'TEST-EXPENSE'"))
        await s.commit()


@pytest.fixture
async def reports_enabled():
    """Reports/export assertions need a plan that actually includes reports."""
    async with async_session_factory() as db:
        old_plan = (
            await db.execute(
                sqltext("SELECT plan FROM tenants WHERE slug = 'kwik-klin'")
            )
        ).scalar_one()
        await db.execute(
            sqltext("UPDATE tenants SET plan = 'growth' WHERE slug = 'kwik-klin'")
        )
        await db.commit()
    try:
        yield
    finally:
        async with async_session_factory() as db:
            await db.execute(
                sqltext("UPDATE tenants SET plan = :plan WHERE slug = 'kwik-klin'"),
                {"plan": old_plan},
            )
            await db.commit()


async def test_bill_with_discount_gst_advance(client) -> None:
    body = {
        "customer_phone": PHONE,
        "customer_name": "Billing Grahak",
        "items": [
            {"type": "Shirt", "qty": 2, "service": "Dry Clean", "rate": 80, "amount": 160, "unit": "pc"},
            {"type": "Wash & Fold (kg)", "qty": 1, "service": "per_kg", "weight_kg": 3, "rate": 60, "amount": 180, "unit": "kg"},
        ],
        "total_amount": "381.20",   # 340 - 20 discount + 61.20 gst
        "discount_amount": "20.00",
        "gst_amount": "61.20",
        "advance_amount": "100.00",
        "advance_method": "UPI",
    }
    r = await client.post("/orders", json=body, headers=AUTH)
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["discount_amount"] == "20.00"
    assert o["gst_amount"] == "61.20"
    assert o["amount_paid"] == "100.00"
    assert o["payment_status"] == "PARTIAL"  # advance recorded as payment
    # kg item survives round-trip with its weight
    kg_items = [i for i in o["items"] if i.get("unit") == "kg"]
    assert kg_items and kg_items[0]["weight_kg"] == 3


async def test_expenses_crud_and_auth(client) -> None:
    assert (await client.get("/admin/api/expenses")).status_code == 401
    r = await client.post(
        "/admin/api/expenses",
        json={"category": "Detergent", "amount": "500", "spent_on": "2026-08-02",
              "description": "TEST-EXPENSE"},
        headers=AUTH,
    )
    assert r.status_code == 201
    eid = r.json()["id"]

    rows = (await client.get("/admin/api/expenses", headers=AUTH)).json()
    assert any(e["id"] == eid for e in rows)

    assert (await client.delete(f"/admin/api/expenses/{eid}", headers=AUTH)).status_code == 200
    rows = (await client.get("/admin/api/expenses", headers=AUTH)).json()
    assert not any(e["id"] == eid for e in rows)


async def test_reports_summary_shape(client, reports_enabled) -> None:
    r = await client.get("/admin/api/reports/summary", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    for period in ("today", "week", "month"):
        for field in ("revenue", "expenses", "profit", "orders"):
            assert field in d[period]
    assert "outstanding_total" in d


async def test_customers_ledger_fields(client) -> None:
    body = {"customer_phone": PHONE, "customer_name": "Billing Grahak",
            "items": [{"type": "Pant", "qty": 1}], "total_amount": "100.00"}
    await client.post("/orders", json=body, headers=AUTH)
    rows = (await client.get("/admin/api/customers", headers=AUTH)).json()
    me = [c for c in rows if c["phone"] == PHONE][0]
    assert float(me["business"]) >= 100
    assert float(me["outstanding"]) >= 100


async def test_rate_card_crud(client) -> None:
    assert (await client.get("/admin/api/rates")).status_code == 401
    r = await client.post(
        "/admin/api/rates",
        json={"service": "TEST Service", "garment": "TEST Kapda", "unit": "pc", "rate": "99"},
        headers=AUTH,
    )
    assert r.status_code == 201
    rid = r.json()["id"]
    try:
        # duplicate service+garment -> 409
        dup = await client.post(
            "/admin/api/rates",
            json={"service": "TEST Service", "garment": "TEST Kapda", "unit": "pc", "rate": "50"},
            headers=AUTH,
        )
        assert dup.status_code == 409
        # update rate + deactivate
        assert (
            await client.put(f"/admin/api/rates/{rid}", json={"rate": "120", "is_active": False}, headers=AUTH)
        ).status_code == 200
        rows = (await client.get("/admin/api/rates", headers=AUTH)).json()
        mine = [x for x in rows if x["id"] == rid][0]
        assert mine["rate"] == "120.00" and mine["is_active"] is False
    finally:
        async with async_session_factory() as s:
            await s.execute(sqltext("DELETE FROM rate_card WHERE service = 'TEST Service'"))
            await s.commit()


async def test_matrix_can_delete_a_rate_a_whole_service_or_a_whole_garment(client) -> None:
    """Galti se bani service/kapda matrix se hat sake — ek cell, poora column,
    poori row. Purane bill apni line mein daam rakhte hain, unpar asar nahi."""
    mk = lambda svc, g: client.post("/admin/api/rates", json={"service": svc, "garment": g, "unit": "pc", "rate": "10"}, headers=AUTH)
    ids = [(await mk(svc, g)).json()["id"] for svc in ("TEST DelSvc", "TEST KeepSvc") for g in ("TEST DelKapda", "TEST KeepKapda")]
    try:
        assert (await client.delete(f"/admin/api/rates/{ids[0]}")).status_code == 401           # bina key nahi
        assert (await client.delete(f"/admin/api/rates/{ids[0]}", headers=AUTH)).status_code == 200
        assert (await client.delete(f"/admin/api/rates/{ids[0]}", headers=AUTH)).status_code == 404
        assert (await client.delete("/admin/api/rates", headers=AUTH)).status_code == 400        # service YA garment
        r = await client.delete("/admin/api/rates?service=TEST%20DelSvc", headers=AUTH)
        assert r.status_code == 200 and r.json()["deleted"] == 1                                  # baaki ek cell
        r = await client.delete("/admin/api/rates?garment=TEST%20DelKapda", headers=AUTH)
        assert r.status_code == 200 and r.json()["deleted"] == 1
        left = {(x["service"], x["garment"]) for x in (await client.get("/admin/api/rates", headers=AUTH)).json()
                if x["service"].startswith("TEST ")}
        assert left == {("TEST KeepSvc", "TEST KeepKapda")}
    finally:
        async with async_session_factory() as s:
            await s.execute(sqltext("DELETE FROM rate_card WHERE service IN ('TEST DelSvc', 'TEST KeepSvc')"))
            await s.commit()


async def test_re_adding_a_disabled_rate_brings_it_back(client) -> None:
    """New Bill se "➕ New item" — band kiya hua kapda dobara jodne par 409
    nahi, wahi row naye rate ke saath chalu ho (unique key use rokti thi)."""
    body = {"service": "TEST Readd", "garment": "TEST Kapda", "unit": "pc", "rate": "40"}
    r = await client.post("/admin/api/rates", json=body, headers=AUTH)
    assert r.status_code == 201
    rid = r.json()["id"]
    try:
        await client.put(f"/admin/api/rates/{rid}", json={"is_active": False}, headers=AUTH)
        again = await client.post("/admin/api/rates", json={**body, "rate": "55"}, headers=AUTH)
        assert again.status_code == 201 and again.json()["id"] == rid
        mine = [x for x in (await client.get("/admin/api/rates", headers=AUTH)).json() if x["id"] == rid][0]
        assert mine["is_active"] is True and mine["rate"] == "55.00"
        # chalu row ab bhi duplicate hi hai
        dup = await client.post("/admin/api/rates", json=body, headers=AUTH)
        assert dup.status_code == 409
    finally:
        async with async_session_factory() as s:
            await s.execute(sqltext("DELETE FROM rate_card WHERE service = 'TEST Readd'"))
            await s.commit()


async def test_staff_settings_crud(client) -> None:
    r = await client.post(
        "/admin/api/staff",
        json={"name": "Test Presser", "phone": "9999900098", "role": "WASHER"},
        headers=AUTH,
    )
    assert r.status_code == 201
    sid = r.json()["id"]
    try:
        # duplicate phone -> 409
        assert (
            await client.post(
                "/admin/api/staff",
                json={"name": "Dup", "phone": "9999900098", "role": "DELIVERY"},
                headers=AUTH,
            )
        ).status_code == 409
        assert (
            await client.put(f"/admin/api/staff/{sid}", json={"role": "DELIVERY", "is_active": False}, headers=AUTH)
        ).status_code == 200
        rows = (await client.get("/admin/api/staff", headers=AUTH)).json()
        mine = [x for x in rows if x["id"] == sid][0]
        assert mine["role"] == "DELIVERY" and mine["is_active"] is False
    finally:
        async with async_session_factory() as s:
            await s.execute(sqltext("DELETE FROM staff WHERE phone = '+919999900098'"))
            await s.commit()


async def test_csv_exports(client, reports_enabled) -> None:
    assert (await client.get("/admin/api/export/orders.csv")).status_code == 401
    r = await client.get("/admin/api/export/orders.csv", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("order_number,")
    r2 = await client.get("/admin/api/export/customers.csv", headers=AUTH)
    assert r2.status_code == 200 and "outstanding" in r2.text.splitlines()[0]
