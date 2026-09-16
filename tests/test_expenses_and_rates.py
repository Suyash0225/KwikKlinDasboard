"""Kharcha aur rate card — staff panel se, aur owner ki apni categories.

Pehre server par hain, isliye test bhi API par:
  - washerman kharcha nahi likh sakta; manager/delivery likh sakte hain
  - staff sirf APNA kharcha dekhe, aur owner ko naam ke saath dikhe
  - category owner ki list se hi — staff apni spelling nahi bana sakta
  - bill banane wala New Bill se rate card mein item jod sake
"""

from datetime import date, timedelta

from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from tests.test_staff_panel import (  # noqa: F401  (two_shops = fixture)
    A_DEL_PHONE,
    A_MGR_PHONE,
    A_PHONE,
    _login,
    two_shops,
)

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


async def _cleanup_tenant_rows(tenant_id) -> None:
    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM expenses WHERE tenant_id = :t"), {"t": str(tenant_id)})
        await db.execute(sqltext("DELETE FROM rate_card WHERE tenant_id = :t"), {"t": str(tenant_id)})
        await db.execute(sqltext("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": str(tenant_id)})
        # bill banne par staff ko work order jaata hai — wo row staff ke
        # delete (fixture teardown) ko rokti
        await db.execute(
            sqltext("DELETE FROM conversations WHERE staff_id IN (SELECT id FROM staff WHERE tenant_id = :t)"),
            {"t": str(tenant_id)},
        )
        await db.commit()


# --- staff: kharcha ----------------------------------------------------------


async def test_washerman_cannot_add_expenses(client, two_shops) -> None:
    await _login(client, A_PHONE)
    me = (await client.get("/staff/api/me")).json()
    assert me["can_expense"] is False
    r = await client.post("/staff/api/expenses", json={"category": "Transport", "amount": 50})
    assert r.status_code == 403


async def test_delivery_adds_expense_and_sees_only_their_own(client, two_shops) -> None:
    try:
        await _login(client, A_MGR_PHONE)
        assert (await client.post(
            "/staff/api/expenses", json={"category": "Detergent", "amount": 300}
        )).status_code == 201
        client.cookies.clear()

        await _login(client, A_DEL_PHONE)
        assert (await client.get("/staff/api/me")).json()["can_expense"] is True
        r = await client.post(
            "/staff/api/expenses",
            json={"category": "transport", "amount": 120, "description": "petrol"},
        )
        assert r.status_code == 201, r.text
        row = r.json()
        assert row["category"] == "Transport"          # list wali spelling
        assert row["added_by"] == "Adel (Delivery)"
        mine = (await client.get("/staff/api/expenses")).json()
        assert [e["amount"] for e in mine["expenses"]] == ["120.00"]   # manager ka nahi
        assert mine["total"] == 120
        assert "Transport" in mine["categories"]
    finally:
        client.cookies.clear()
        await _cleanup_tenant_rows(two_shops["a"])


async def test_staff_cannot_invent_categories_or_backdate(client, two_shops) -> None:
    try:
        await _login(client, A_DEL_PHONE)
        bad = await client.post("/staff/api/expenses", json={"category": "Party", "amount": 500})
        assert bad.status_code == 400
        old = (date.today() - timedelta(days=30)).isoformat()
        r = await client.post(
            "/staff/api/expenses", json={"category": "Transport", "amount": 50, "spent_on": old}
        )
        assert r.status_code == 400
        future = (date.today() + timedelta(days=3)).isoformat()
        r = await client.post(
            "/staff/api/expenses", json={"category": "Transport", "amount": 50, "spent_on": future}
        )
        assert r.status_code == 400
    finally:
        client.cookies.clear()
        await _cleanup_tenant_rows(two_shops["a"])


# --- staff: rate card se naya item -------------------------------------------


async def test_biller_adds_a_new_item_from_the_bill_screen(client, two_shops) -> None:
    try:
        await _login(client, A_DEL_PHONE)
        body = {"service": "Dry Clean", "garment": "Blazer", "unit": "pc", "rate": 180}
        r = await client.post("/staff/api/rates", json=body)
        assert r.status_code == 201, r.text
        rates = (await client.get("/staff/api/rates")).json()
        assert {"service": "Dry Clean", "garment": "Blazer", "unit": "pc", "rate": 180.0} in rates
        assert (await client.post("/staff/api/rates", json=body)).status_code == 409
        # jo item joda wahi bill par chalna chahiye
        bill = await client.post("/staff/api/bills", json={
            "customer_phone": "9999900204", "customer_name": "Grahak",
            "items": [{"service": "Dry Clean", "garment": "Blazer", "qty": 2}],
        })
        assert bill.status_code in (200, 201), bill.text
        assert bill.json()["total"] == 360
    finally:
        client.cookies.clear()
        await _cleanup_tenant_rows(two_shops["a"])


async def test_washerman_cannot_touch_the_rate_card(client, two_shops) -> None:
    await _login(client, A_PHONE)
    r = await client.post(
        "/staff/api/rates", json={"service": "X", "garment": "Y", "unit": "pc", "rate": 1}
    )
    assert r.status_code == 403


# --- KG line ke kapde (BUG_005) ----------------------------------------------


async def _kg_rate(tenant_id, service="TEST Wash (kg)", rate="60") -> None:
    async with async_session_factory() as db:
        await db.execute(
            sqltext(
                "INSERT INTO rate_card (id, tenant_id, service, garment, unit, rate, is_active)"
                " VALUES (gen_random_uuid(), :t, :s, '', 'kg', :r, true)"
            ),
            {"t": str(tenant_id), "s": service, "r": rate},
        )
        await db.commit()


async def test_staff_bills_a_kg_service_with_a_cloth_count(client, two_shops) -> None:
    """Pehle panel se kg service ka bill 422 par girta tha (garment khali).
    Ab ban-ta hai, aur bore ke kapde ginti ke saath item par baithte hain."""
    await _kg_rate(two_shops["a"])
    try:
        await _login(client, A_DEL_PHONE)
        r = await client.post("/staff/api/bills", json={
            "customer_phone": "9999900204", "customer_name": "Grahak",
            "items": [
                {"service": "TEST Wash (kg)", "garment": "", "qty": 3.5,
                 "pieces": [{"type": " Shirt ", "qty": 5}, {"type": "Pant", "qty": 3}]},
            ],
        })
        assert r.status_code == 201, r.text
        assert r.json()["total"] == 210                       # 3.5 x 60 — ginti se daam nahi
        number = r.json()["order_number"]
        async with async_session_factory() as db:
            items = (await db.execute(
                sqltext("SELECT items FROM orders WHERE order_number = :n AND tenant_id = :t"),
                {"n": number, "t": str(two_shops["a"])},
            )).scalar_one()
        line = items[0]
        assert line["unit"] == "kg" and line["type"] == "TEST Wash (kg)"
        assert line["pieces"] == [{"type": "Shirt", "qty": 5}, {"type": "Pant", "qty": 3}]
        receipt = (await client.get(f"/staff/api/orders/{number}/receipt")).json()
        assert "3.5 kg x ₹60 = ₹210" in receipt["text"]
        assert "  Shirt 5\n  Pant 3\n  Total clothes: 8" in receipt["text"]
        # printer wala: ASCII, 58mm = 32 akshar, kg wali shart apne aap
        pt = receipt["print_text"]
        assert receipt["paper_mm"] == 58 and pt.isascii()
        assert all(len(ln) <= 32 for ln in pt.splitlines())
        assert "Rs.210" in pt and "Kg services" in pt
    finally:
        client.cookies.clear()
        await _cleanup_tenant_rows(two_shops["a"])


async def test_staff_adds_a_kg_service_without_an_item_name_and_bills_it(client, two_shops) -> None:
    """BUG_006: kg service (jaise "Premium Wash") ka item naam hota hi nahi."""
    try:
        await _login(client, A_DEL_PHONE)
        r = await client.post("/staff/api/rates", json={
            "service": "Premium Wash", "garment": "", "unit": "kg", "rate": 90,
        })
        assert r.status_code == 201, r.text
        assert r.json()["garment"] == "" and r.json()["unit"] == "kg"
        # per-piece service par naam zaroori
        bad = await client.post("/staff/api/rates", json={
            "service": "Dry Clean", "garment": "", "unit": "pc", "rate": 90,
        })
        assert bad.status_code == 422
        bill = await client.post("/staff/api/bills", json={
            "customer_phone": "9999900204",
            "items": [{"service": "Premium Wash", "garment": "", "qty": 2.5}],
        })
        assert bill.status_code == 201, bill.text
        assert bill.json()["total"] == 225
    finally:
        client.cookies.clear()
        await _cleanup_tenant_rows(two_shops["a"])


async def test_pieces_are_ignored_on_per_piece_lines(client, two_shops) -> None:
    try:
        await _login(client, A_DEL_PHONE)
        assert (await client.post("/staff/api/rates", json={
            "service": "Dry Clean", "garment": "Kurta", "unit": "pc", "rate": 80,
        })).status_code == 201
        r = await client.post("/staff/api/bills", json={
            "customer_phone": "9999900204",
            "items": [{"service": "Dry Clean", "garment": "Kurta", "qty": 2,
                       "pieces": [{"type": "Shirt", "qty": 9}]}],
        })
        assert r.status_code == 201, r.text
        async with async_session_factory() as db:
            items = (await db.execute(
                sqltext("SELECT items FROM orders WHERE order_number = :n AND tenant_id = :t"),
                {"n": r.json()["order_number"], "t": str(two_shops["a"])},
            )).scalar_one()
        assert "pieces" not in items[0]
    finally:
        client.cookies.clear()
        await _cleanup_tenant_rows(two_shops["a"])


async def test_dashboard_bill_keeps_pieces_and_rejects_junk(client) -> None:
    from tests.conftest import purge_phones

    phone = "+919999900078"
    try:
        good = await client.post("/orders", json={
            "customer_phone": phone, "total_amount": "180",
            "items": [{"type": "Wash & Fold (kg)", "service": "Wash & Fold (kg)", "qty": 3,
                       "rate": 60, "amount": 180, "unit": "kg",
                       "pieces": [{"type": "Towel", "qty": 4}]}],
        }, headers=AUTH)
        assert good.status_code == 201, good.text
        assert good.json()["items"][0]["pieces"] == [{"type": "Towel", "qty": 4}]
        bad = await client.post("/orders", json={
            "customer_phone": phone, "total_amount": "180",
            "items": [{"type": "Wash & Fold (kg)", "qty": 3, "pieces": [{"type": "Towel", "qty": 0}]}],
        }, headers=AUTH)
        assert bad.status_code == 422
    finally:
        await purge_phones(phone)


def test_items_summary_shows_the_cloth_count() -> None:
    from types import SimpleNamespace

    from app.services.work_orders import items_summary

    order = SimpleNamespace(items=[
        {"type": "Wash & Fold (kg)", "qty": 3.5, "pieces": [{"type": "Shirt", "qty": 5}, {"type": "Pant", "qty": 3}]},
        {"type": "Saree", "qty": 1},
    ])
    assert items_summary(order) == "3.5 x Wash & Fold (kg) (Shirt 5, Pant 3 — 8 pcs), 1 x Saree"


# --- owner: apni categories --------------------------------------------------


async def test_owner_custom_category_round_trip(client) -> None:
    name = "TEST Packaging"
    try:
        r = await client.post("/admin/api/expense-categories", json={"name": f"  {name} "}, headers=AUTH)
        assert r.status_code == 201, r.text
        assert r.json()["name"] == name
        cats = (await client.get("/admin/api/expense-categories", headers=AUTH)).json()
        assert name in cats["custom"] and cats["all"][-1] == "Other"
        # dobara, alag case mein — duplicate
        dup = await client.post("/admin/api/expense-categories", json={"name": name.lower()}, headers=AUTH)
        assert dup.status_code == 400
        # built-in nahi hatti
        assert (await client.delete("/admin/api/expense-categories/Rent", headers=AUTH)).status_code == 400
        # kharcha is category mein, alag spelling se bhi usi khaane mein
        e = await client.post("/admin/api/expenses", json={
            "category": name.upper(), "amount": "75", "spent_on": date.today().isoformat(),
        }, headers=AUTH)
        assert e.status_code == 201
        rows = (await client.get("/admin/api/expenses", headers=AUTH)).json()
        assert any(x["id"] == e.json()["id"] and x["category"] == name for x in rows)
        # hatao — purana kharcha waisa ka waisa
        gone = await client.delete(f"/admin/api/expense-categories/{name}", headers=AUTH)
        assert gone.status_code == 200 and name not in gone.json()["all"]
        rows = (await client.get("/admin/api/expenses", headers=AUTH)).json()
        assert any(x["id"] == e.json()["id"] for x in rows)
    finally:
        async with async_session_factory() as s:
            await s.execute(sqltext("DELETE FROM expenses WHERE category = :c"), {"c": name})
            await s.commit()
        await client.delete(f"/admin/api/expense-categories/{name}", headers=AUTH)
