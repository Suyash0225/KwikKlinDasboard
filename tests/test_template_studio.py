"""Template Studio + auto-message templates — always on the shop's OWN WABA.

Pehle studio .env ka WABA/token use karta tha: har dukaan ka template home
dukaan ke account par banta aur har dukaan home ki list dekhti.
"""

import pytest
from sqlalchemy import text as sqltext

import app.services.wa_templates as wa_templates
from app.config import settings
from app.database import async_session_factory
from app.models import ROLE_OWNER, User
from app.models.tenant import Tenant
from app.routers.orders import vendor_master_key
from app.services import auth, tenant_context

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
CTL = {"X-API-Key": vendor_master_key()}

E_SLUG, E_WABA, E_TOKEN, E_EMAIL = "test-wa-tpl-e", "waba-e-0001", "EAAtokenForShopE000000000000", "wa-tpl-e@test.local"
F_SLUG, F_EMAIL = "test-wa-tpl-f", "wa-tpl-f@test.local"


@pytest.fixture
def graph(monkeypatch):
    calls: list[dict] = []

    async def fake_graph(method, path, token=None, **kw):
        calls.append({"method": method, "path": path, "token": token, **kw})
        if method == "GET":
            return 200, {"data": [
                {"name": "kk_dyn_ready", "status": "APPROVED", "category": "UTILITY",
                 "language": "en_US",
                 "components": [{"type": "BODY", "text": "Order {{1}} ready hai"}]},
                # Meta par approved, "View bill" button ke saath (spec jaisa)
                {"name": "kk_order_ready", "status": "APPROVED", "category": "UTILITY",
                 "language": "en_US", "components": [
                     {"type": "BODY", "text": "x {{1}} y"},
                     {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "View bill", "url": "https://kwikklin.online/b/{{1}}"}]}]},
                # Purana approved template bina button ke -> NEEDS_UPDATE
                {"name": "kk_partial_delivery", "status": "APPROVED", "category": "UTILITY",
                 "language": "en_US", "components": [{"type": "BODY", "text": "p {{1}} {{2}} {{3}}"}]},
                {"name": "kk_rejected_one", "status": "REJECTED", "category": "MARKETING",
                 "language": "en_US", "rejected_reason": "INVALID_FORMAT",
                 "components": [{"type": "BODY", "text": "Offer!"}]},
            ]}
        if method == "POST":
            return 200, {"status": "PENDING", "id": "123"}
        return 200, {"success": True}

    monkeypatch.setattr(wa_templates, "graph", fake_graph)
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN", "EAAhomeTokenForTests")
    monkeypatch.setattr(settings, "WHATSAPP_WABA_ID", "999000")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "111000111000")
    return calls


async def _shop(slug, email, **wa):
    async with async_session_factory() as db:
        t = Tenant(slug=slug, shop_name=slug, owner_name="O", plan="growth",
                   owner_phone="+919999900066", status="active", **wa)
        db.add(t)
        await db.commit()
        await db.refresh(t)
        u = User(tenant_id=t.id, email=email, name="Owner",
                 password_hash=auth.hash_password("tplpass123"), role=ROLE_OWNER)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return t, await auth.start_session(db, u)


@pytest.fixture
async def shops(client):
    e, e_tok = await _shop(E_SLUG, E_EMAIL, wa_phone_number_id="555000666777",
                           wa_waba_id=E_WABA, wa_token=E_TOKEN)
    f, f_tok = await _shop(F_SLUG, F_EMAIL)
    try:
        yield {"e": e_tok, "f": f_tok}
    finally:
        client.cookies.delete(auth.SESSION_COOKIE)
        tenant_context.current_tenant_id.set(None)
        async with async_session_factory() as db:
            for slug, email in ((E_SLUG, E_EMAIL), (F_SLUG, F_EMAIL)):
                await db.execute(sqltext("DELETE FROM audit_log WHERE tenant_id IN (SELECT id FROM tenants WHERE slug=:s)"), {"s": slug})
                await db.execute(sqltext("DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users WHERE email=:e)"), {"e": email})
                await db.execute(sqltext("DELETE FROM users WHERE email=:e"), {"e": email})
                await db.execute(sqltext("DELETE FROM tenants WHERE slug=:s"), {"s": slug})
            await db.commit()


async def test_list_returns_local_registry_for_waha(client, graph) -> None:
    r = await client.get("/admin/api/templates", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    names = {x["name"] for x in rows}
    assert "kk_order_ready" in names
    assert "kk_payment_reminder" in names
    assert all(x["status"] == "UNKNOWN" for x in rows)
    assert graph == [], "WAHA template registry must not contact Meta"
    from app.services.templates import build_template

    assert build_template("kk_order_ready", ["KK-20260803-01"])["name"] == "kk_order_ready"


@pytest.mark.skipif(settings.WHATSAPP_PROVIDER != "meta", reason="Meta template submission is unavailable in WAHA/NOWEB mode")
async def test_create_validates_and_submits(client, graph) -> None:
    r = await client.post("/admin/api/templates", headers=AUTH, json={
        "name": "My Offer!", "category": "MARKETING",
        "body": "Namaste {{1}}, 10% off!", "samples": [],
    })
    assert r.status_code == 400 and "sample" in r.json()["detail"].lower()

    r = await client.post("/admin/api/templates", headers=AUTH, json={
        "name": "My Offer!", "category": "MARKETING",
        "body": "Namaste {{1}}, 10% off!", "samples": ["Sharma ji"],
        "footer": "Kwik Klin",
        "buttons": [{"type": "URL", "text": "Order karein", "url": "https://wa.me/919696856069"}],
    })
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "my_offer_"
    comps = [c for c in graph if c["method"] == "POST"][0]["json"]["components"]
    assert any(c["type"] == "BUTTONS" for c in comps)
    assert any(c["type"] == "FOOTER" for c in comps)

    r = await client.post("/admin/api/templates", headers=AUTH, json={
        "name": "bad_vars", "category": "UTILITY", "body": "Hello {{2}}", "samples": ["x"],
    })
    assert r.status_code == 400


@pytest.mark.skipif(settings.WHATSAPP_PROVIDER != "meta", reason="Meta template deletion is unavailable in WAHA/NOWEB mode")
async def test_delete_template(client, graph) -> None:
    r = await client.delete("/admin/api/templates/kk_old_one", headers=AUTH)
    assert r.status_code == 200 and r.json()["deleted"] == "kk_old_one"


async def test_studio_uses_local_registry_for_logged_in_shop(client, graph, shops) -> None:
    client.cookies.set(auth.SESSION_COOKIE, shops["e"])
    r = await client.get("/admin/api/templates", headers=AUTH)
    assert r.status_code == 200
    assert any(x["name"] == "kk_order_ready" for x in r.json())
    assert graph == [], "WAHA Template Studio must not contact Meta"


async def test_unconnected_shop_never_touches_home_waba(client, graph, shops) -> None:
    client.cookies.set(auth.SESSION_COOKIE, shops["f"])

    r = await client.get("/admin/api/templates", headers=AUTH)
    assert r.status_code == 200
    assert any(x["name"] == "kk_order_ready" for x in r.json())

    r = await client.post("/admin/api/templates", headers=AUTH, json={
        "name": "leak_try", "category": "UTILITY",
        "body": "Hello there {{1}} ok", "samples": ["x"],
    })
    assert r.status_code == 400 and "not connected" in r.json()["detail"]

    r = await client.delete("/admin/api/templates/kk_order_ready", headers=AUTH)
    assert r.status_code == 400 and "not connected" in r.json()["detail"]

    assert graph == [], "unconnected shop reached Meta (home creds leaked)"


async def test_control_template_registry_is_local_for_each_shop(client, graph, shops) -> None:
    r = await client.get(f"/control/api/tenants/{E_SLUG}/whatsapp/templates", headers=CTL)
    assert r.status_code == 200
    payload = r.json()
    assert payload["state"] == "local"
    by_name = {t["name"]: t["status"] for t in payload["templates"]}
    assert by_name["kk_order_ready"] == "LOCAL"
    assert by_name["kk_picked_up"] == "LOCAL"
    assert graph == []

    r = await client.post(f"/control/api/tenants/{E_SLUG}/whatsapp/templates", headers=CTL)
    assert r.status_code == 503
    assert "WAHA/NOWEB" in r.json()["detail"]

    r = await client.get(f"/control/api/tenants/{F_SLUG}/whatsapp/templates", headers=CTL)
    assert r.status_code == 200
    assert r.json()["state"] == "local"
    assert graph == [], "Control Room template registry must not contact Meta"


async def test_owner_cannot_connect_whatsapp_themselves(client, shops) -> None:
    client.cookies.set(auth.SESSION_COOKIE, shops["f"])
    r = await client.post("/api/whatsapp/connect", json={
        "phone_number_id": "555000111222", "token": "EAA" + "x" * 25})
    assert r.status_code in (404, 405)
    assert (await client.get("/api/whatsapp/status")).json()["connected"] is False
