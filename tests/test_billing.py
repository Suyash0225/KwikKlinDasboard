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


async def test_reports_summary_shape(client) -> None:
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


async def test_rate_card_rejects_same_name_in_other_case(client) -> None:
    """"shirt" aur "Shirt" ek hi kapda hai — do rows kabhi nahi."""
    mk = lambda g, r: {"service": "TEST Dhulai", "garment": g, "unit": "pc", "rate": r}  # noqa: E731
    assert (await client.post("/admin/api/rates", json=mk("TEST Kurta", "80"), headers=AUTH)).status_code == 201
    try:
        for typed in ("test kurta", "  TEST KURTA  "):
            dup = await client.post("/admin/api/rates", json=mk(typed, "40"), headers=AUTH)
            assert dup.status_code == 409, typed
            assert "already on the rate card" in dup.json()["detail"]
        rows = [x for x in (await client.get("/admin/api/rates", headers=AUTH)).json()
                if x["service"] == "TEST Dhulai"]
        assert len(rows) == 1 and rows[0]["rate"] == "80.00"

        # Band row par dobara daam daalo -> wahi row zinda ho, nayi na bane
        await client.put(f"/admin/api/rates/{rows[0]['id']}", json={"is_active": False}, headers=AUTH)
        again = await client.post("/admin/api/rates", json=mk("test kurta", "95"), headers=AUTH)
        assert again.status_code == 201 and again.json()["id"] == rows[0]["id"]
        rows = [x for x in (await client.get("/admin/api/rates", headers=AUTH)).json()
                if x["service"] == "TEST Dhulai"]
        assert len(rows) == 1 and rows[0]["is_active"] is True and rows[0]["rate"] == "95.00"
    finally:
        async with async_session_factory() as s:
            await s.execute(sqltext("DELETE FROM rate_card WHERE service = 'TEST Dhulai'"))
            await s.commit()


async def test_rate_card_delete(client) -> None:
    """Ek cell, poora kapda, poori service — teeno hatane ka raasta."""
    async def add(service: str, garment: str) -> str:
        r = await client.post(
            "/admin/api/rates",
            json={"service": service, "garment": garment, "unit": "pc", "rate": "70"},
            headers=AUTH,
        )
        assert r.status_code == 201
        return r.json()["id"]

    async def mine() -> list[dict]:
        rows = (await client.get("/admin/api/rates", headers=AUTH)).json()
        return [x for x in rows if x["service"].startswith("TEST Del")]

    one = await add("TEST Del A", "TEST Topi")
    await add("TEST Del B", "TEST Topi")
    await add("TEST Del B", "TEST Mojaa")
    try:
        # 1. ek cell
        assert (await client.delete(f"/admin/api/rates/{one}", headers=AUTH)).status_code == 200
        assert (await client.delete(f"/admin/api/rates/{one}", headers=AUTH)).status_code == 404
        assert len(await mine()) == 2

        # 2. poora kapda — naam ka case maayne nahi rakhta
        r = await client.delete("/admin/api/rates?garment=test%20topi", headers=AUTH)
        assert r.status_code == 200 and r.json()["deleted"] == 1
        assert [x["garment"] for x in await mine()] == ["TEST Mojaa"]

        # 3. poori service
        r = await client.delete("/admin/api/rates?service=TEST%20Del%20B", headers=AUTH)
        assert r.status_code == 200 and r.json()["deleted"] == 1
        assert await mine() == []

        # kuch match na ho to 404, aur khaali sawaal par 400
        assert (await client.delete("/admin/api/rates?garment=TEST%20Nahi", headers=AUTH)).status_code == 404
        assert (await client.delete("/admin/api/rates", headers=AUTH)).status_code == 400
    finally:
        async with async_session_factory() as s:
            await s.execute(sqltext("DELETE FROM rate_card WHERE service LIKE 'TEST Del%'"))
            await s.commit()


async def test_rate_card_rename(client) -> None:
    """Naam badlo — kapda ho ya service, uski saari rows ek saath."""
    async def add(service: str, garment: str) -> None:
        r = await client.post(
            "/admin/api/rates",
            json={"service": service, "garment": garment, "unit": "pc", "rate": "60"},
            headers=AUTH,
        )
        assert r.status_code == 201

    async def mine() -> list[dict]:
        rows = (await client.get("/admin/api/rates", headers=AUTH)).json()
        return [x for x in rows if x["service"].startswith("TEST Rn")]

    await add("TEST Rn Wash", "TEST Chadar")
    await add("TEST Rn Iron", "TEST Chadar")
    await add("TEST Rn Wash", "TEST Takiya")
    try:
        # kapda: dono rows ka naam badla, service waisi ki waisi
        r = await client.put(
            "/admin/api/rates/rename",
            json={"kind": "garment", "old": "test chadar", "new": "TEST Bedsheet"},
            headers=AUTH,
        )
        assert r.status_code == 200 and r.json()["renamed"] == 2
        assert sorted({x["garment"] for x in await mine()}) == ["TEST Bedsheet", "TEST Takiya"]

        # service: uski saari rows
        r = await client.put(
            "/admin/api/rates/rename",
            json={"kind": "service", "old": "TEST Rn Wash", "new": "TEST Rn Dhulai"},
            headers=AUTH,
        )
        assert r.status_code == 200 and r.json()["renamed"] == 2

        # sirf case badalna chalta hai
        assert (
            await client.put(
                "/admin/api/rates/rename",
                json={"kind": "garment", "old": "TEST Bedsheet", "new": "TEST BEDSHEET"},
                headers=AUTH,
            )
        ).status_code == 200

        # maujooda doosre naam par le jaana -> 409, kyunki rows takrayengi
        clash = await client.put(
            "/admin/api/rates/rename",
            json={"kind": "garment", "old": "TEST Takiya", "new": "test bedsheet"},
            headers=AUTH,
        )
        assert clash.status_code == 409

        # jo card par hai hi nahi
        assert (
            await client.put(
                "/admin/api/rates/rename",
                json={"kind": "garment", "old": "TEST Nahi Hai", "new": "TEST Kuch"},
                headers=AUTH,
            )
        ).status_code == 404

        # rename route ko {rate_id} nigal na le -- ye asli regression hai
        assert (
            await client.put("/admin/api/rates/rename", json={}, headers=AUTH)
        ).status_code == 422
    finally:
        async with async_session_factory() as s:
            await s.execute(sqltext("DELETE FROM rate_card WHERE service LIKE 'TEST Rn%'"))
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


async def test_csv_exports(client) -> None:
    assert (await client.get("/admin/api/export/orders.csv")).status_code == 401
    r = await client.get("/admin/api/export/orders.csv", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("order_number,")
    r2 = await client.get("/admin/api/export/customers.csv", headers=AUTH)
    assert r2.status_code == 200 and "outstanding" in r2.text.splitlines()[0]
