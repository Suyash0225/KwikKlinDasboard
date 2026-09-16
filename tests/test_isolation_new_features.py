"""Aaj ke naye features — do dukaan, ek doosre ka kuch nahi dekh/badal sakti.

Kharcha + categories, rate card, order detail/receipt/message/deliver,
review link aur WhatsApp creds. Staff panel (cookie) aur owner dashboard
(kk_session) dono raaste. Asli guarantee NOSUPERUSER role (APP_DATABASE_URL)
par hi poori hoti hai — superuser par RLS bypass hota hai, ORM filter tab
bhi yahi tests paas karwata hai.
"""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models import ROLE_OWNER, StaffRole
from app.models.tenant import Tenant, User
from app.services import app_settings, auth, tenant_context
from tests.test_staff_panel import _order_for, _purge_staff, _staff, _tenant, purge_phones

A_SLUG, B_SLUG = "iso-new-a", "iso-new-b"
A_MGR, B_MGR = "+919999900261", "+919999900262"
CUST_A, CUST_B = "+919999900263", "+919999900264"
A_OWNER, B_OWNER = "iso-new-a@test.local", "iso-new-b@test.local"
PW = "khulja-sim-sim"


async def _owner_session(tid, email) -> str:
    async with async_session_factory() as db:
        u = User(tenant_id=tid, email=email, name="Owner",
                 password_hash=auth.hash_password("iso-owner-pw1"), role=ROLE_OWNER)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return await auth.start_session(db, u)


async def _in_shop(tid, fn):
    tok = tenant_context.current_tenant_id.set(tid)
    try:
        async with async_session_factory() as db:
            return await fn(db)
    finally:
        tenant_context.current_tenant_id.reset(tok)


async def _wipe() -> None:
    tenant_context.current_tenant_id.set(None)
    await _purge_staff(A_MGR, B_MGR)
    await purge_phones(CUST_A, CUST_B)
    async with async_session_factory() as db:
        for q in (
            "DELETE FROM expenses WHERE tenant_id IN (SELECT id FROM tenants WHERE slug IN (:a,:b))",
            "DELETE FROM rate_card WHERE tenant_id IN (SELECT id FROM tenants WHERE slug IN (:a,:b))",
            "DELETE FROM settings_kv WHERE tenant_id IN (SELECT id FROM tenants WHERE slug IN (:a,:b))",
            "DELETE FROM audit_log WHERE tenant_id IN (SELECT id FROM tenants WHERE slug IN (:a,:b))",
            "DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users WHERE email IN (:ea,:eb))",
            "DELETE FROM users WHERE email IN (:ea,:eb)",
            "DELETE FROM tenants WHERE slug IN (:a,:b)",
        ):
            await db.execute(sqltext(q), {"a": A_SLUG, "b": B_SLUG, "ea": A_OWNER, "eb": B_OWNER})
        await db.commit()


@pytest.fixture
async def shops():
    await _wipe()
    a, b = await _tenant(A_SLUG, "pro"), await _tenant(B_SLUG, "pro")
    ids = {
        "a": a, "b": b,
        "a_mgr": await _staff(a, A_MGR, "IsoAmgr", StaffRole.MANAGER),
        "b_mgr": await _staff(b, B_MGR, "IsoBmgr", StaffRole.MANAGER),
        "a_owner": await _owner_session(a, A_OWNER),
        "b_owner": await _owner_session(b, B_OWNER),
    }
    yield ids
    await _wipe()


async def _staff_login(client, phone):
    client.cookies.clear()
    r = await client.post("/staff/api/login", json={"phone": phone, "password": PW})
    assert r.status_code == 200, r.text


def _owner(client, token):
    client.cookies.clear()
    client.cookies.set(auth.SESSION_COOKIE, token)


async def test_expenses_and_categories_stay_in_their_shop(client, shops) -> None:
    _owner(client, shops["b_owner"])
    r = await client.post("/admin/api/expense-categories", json={"name": "BOnlyCat"})
    assert r.status_code == 201, r.text

    await _staff_login(client, A_MGR)
    cats = (await client.get("/staff/api/expenses")).json()["categories"]
    assert "BOnlyCat" not in cats, "shop B's category showed up in shop A"
    r = await client.post("/staff/api/expenses", json={"category": "BOnlyCat", "amount": 10})
    assert r.status_code == 400
    r = await client.post("/staff/api/expenses", json={"category": cats[0], "amount": 123.45,
                                                        "description": "iso-a-petrol"})
    assert r.status_code == 201, r.text
    exp_id = r.json()["id"]

    _owner(client, shops["b_owner"])
    assert "iso-a-petrol" not in (await client.get("/admin/api/expenses")).text
    assert (await client.delete(f"/admin/api/expenses/{exp_id}")).status_code == 404

    _owner(client, shops["a_owner"])
    assert "iso-a-petrol" in (await client.get("/admin/api/expenses")).text
    all_a = (await client.get("/admin/api/expense-categories")).json()["all"]
    assert "BOnlyCat" not in all_a


async def test_rate_added_by_one_shop_is_invisible_to_the_other(client, shops) -> None:
    await _staff_login(client, A_MGR)
    r = await client.post("/staff/api/rates", json={"service": "IsoWashA", "garment": "IsoKurta",
                                                     "unit": "pc", "rate": 55})
    assert r.status_code == 201, r.text

    await _staff_login(client, B_MGR)
    assert "IsoWashA" not in (await client.get("/staff/api/rates")).text
    # same naam B mein bhi ban sakta hai — A ka rate B ke liye "duplicate" nahi
    r = await client.post("/staff/api/rates", json={"service": "IsoWashA", "garment": "IsoKurta",
                                                     "unit": "pc", "rate": 99})
    assert r.status_code == 201, r.text

    _owner(client, shops["a_owner"])
    rows = [x for x in (await client.get("/admin/api/rates")).json() if x.get("service") == "IsoWashA"]
    assert [float(x["rate"]) for x in rows] == [55.0], rows


async def test_other_shops_order_is_404_everywhere(client, shops, sent) -> None:
    a_num = await _order_for(shops["a"], shops["a_mgr"], CUST_A, delivery=True, total=300)
    b_num = await _order_for(shops["b"], shops["b_mgr"], CUST_B, delivery=True, total=200)
    if a_num == b_num:
        # har dukaan ki apni ginti — dono KK-..-01 ho sakte hain; tab A ka
        # doosra bill lo jo B mein hai hi nahi
        a_num = await _order_for(shops["a"], shops["a_mgr"], CUST_A, delivery=True, total=300)

    await _staff_login(client, B_MGR)
    for method, path in (
        ("GET", f"/staff/api/orders/{a_num}"),
        ("GET", f"/staff/api/orders/{a_num}/receipt"),
        ("GET", f"/staff/api/orders/{a_num}/message?kind=service_thanks"),
        ("GET", f"/staff/api/orders/{a_num}/call"),
        ("POST", f"/staff/api/orders/{a_num}/deliver"),
        ("POST", f"/staff/api/orders/{a_num}/ready"),
    ):
        r = await client.request(method, path, json={} if method == "POST" else None)
        assert r.status_code in (403, 404), f"{method} {path} -> {r.status_code}"
        assert CUST_A not in r.text and "Route Grahak" not in r.text or r.status_code == 404

    _owner(client, shops["b_owner"])
    for method, path in (
        ("GET", f"/orders/{a_num}"),
        ("GET", f"/orders/{a_num}/receipt"),
        ("GET", f"/orders/{a_num}/message?kind=service_thanks"),
        ("POST", f"/orders/{a_num}/deliver"),
    ):
        r = await client.request(method, path, json={} if method == "POST" else None)
        assert r.status_code == 404, f"{method} {path} -> {r.status_code} {r.text[:120]}"

    # A ka order jyon ka tyon
    _owner(client, shops["a_owner"])
    r = await client.get(f"/orders/{a_num}/receipt")
    assert r.status_code == 200 and r.json()["phone"] == CUST_A


async def test_receipt_and_review_link_use_own_shop(client, shops, monkeypatch) -> None:
    async def _set(db):
        await app_settings.set_value(db, "google_review_link", "https://g.page/r/iso-a-review")
        await app_settings.set_value(db, "public_base_url", "https://kk.example")
    await _in_shop(shops["a"], _set)

    r = await client.get(f"/r/{A_SLUG}", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and "iso-a-review" in r.headers["location"]
    r = await client.get(f"/r/{B_SLUG}", follow_redirects=False)
    assert "iso-a-review" not in r.headers.get("location", ""), "B's short link opened A's Google page"

    from app.services import customer_messages

    async def _link(db):
        return await customer_messages.short_review_link(db, await db.get(Tenant, tenant_context.current_tenant_id.get()))
    assert await _in_shop(shops["a"], _link) == f"https://kk.example/r/{A_SLUG}"
    assert await _in_shop(shops["b"], _link) == ""


async def test_whatsapp_never_borrows_another_shops_number(shops, monkeypatch) -> None:
    from app.services import wa_templates, whatsapp

    monkeypatch.setattr(settings, "WHATSAPP_TOKEN", "EAAhomeTokenForTests")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "111000111000")
    monkeypatch.setattr(settings, "WHATSAPP_WABA_ID", "waba-home")
    async with async_session_factory() as db:
        await db.execute(sqltext(
            "UPDATE tenants SET wa_phone_number_id='555000262626', wa_waba_id='waba-iso-a',"
            " wa_token=NULL WHERE slug=:s"), {"s": A_SLUG})
        await db.commit()
        ta = (await db.execute(sqltext("SELECT id FROM tenants WHERE slug=:s"), {"s": A_SLUG})).scalar_one()
        a = await db.get(Tenant, ta)
        a.wa_token = "EAAtokenIsoShopA000000000000"
        await db.commit()
        b = await db.get(Tenant, shops["b"])

        assert whatsapp.shop_can_send(a) is True
        assert whatsapp.shop_can_send(b) is False
        assert wa_templates.creds_for(a).token == "EAAtokenIsoShopA000000000000"
        assert wa_templates.creds_for(b) is None

    async def _resolve(db):
        return await whatsapp.resolve_creds(db)
    assert (await _in_shop(shops["a"], _resolve)).phone_number_id == "555000262626"
    with pytest.raises(whatsapp.SendError):
        await _in_shop(shops["b"], _resolve)
