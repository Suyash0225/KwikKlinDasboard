"""Instagram + Google Business: token ka har kaam sirf Control panel se.

Owner dashboard token likh/jod nahi sakta, token DB mein encrypted rehta hai,
browser tak kabhi nahi jaata, aur ek dukaan ka connection doosri par nahi
likhta. Meta/Google ko kabhi call nahi — check/exchange functions nakli.
"""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models import ROLE_OWNER
from app.models.tenant import Tenant, User
from app.routers.orders import vendor_master_key
from app.services import auth, google_business as gbp, integrations, secrets, tenant_context

CTL = {"X-API-Key": vendor_master_key()}
A, B = "test-integ-a", "test-integ-b"
A_EMAIL = "integ-a@test.local"
IG_TOKEN = "IGQVJtestTokenForShopA0000000000"


async def _wipe():
    tenant_context.current_tenant_id.set(None)
    async with async_session_factory() as db:
        for q in (
            "DELETE FROM settings_kv WHERE tenant_id IN (SELECT id FROM tenants WHERE slug IN (:a,:b))",
            "DELETE FROM audit_log WHERE tenant_id IN (SELECT id FROM tenants WHERE slug IN (:a,:b))",
            "DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users WHERE email = :e)",
            "DELETE FROM users WHERE email = :e",
            "DELETE FROM tenants WHERE slug IN (:a,:b)",
        ):
            await db.execute(sqltext(q), {"a": A, "b": B, "e": A_EMAIL})
        await db.commit()


@pytest.fixture
async def shops(client):
    await _wipe()
    async with async_session_factory() as db:
        ta = Tenant(slug=A, shop_name="Integ A", owner_name="A", plan="growth",
                    owner_phone="+919999900271", status="active")
        tb = Tenant(slug=B, shop_name="Integ B", owner_name="B", plan="growth",
                    owner_phone="+919999900272", status="active")
        db.add_all([ta, tb])
        await db.commit()
        u = User(tenant_id=ta.id, email=A_EMAIL, name="A Owner",
                 password_hash=auth.hash_password("integ-pass-1"), role=ROLE_OWNER)
        db.add(u)
        await db.commit()
        token = await auth.start_session(db, u)
        ids = {"a": ta.id, "b": tb.id, "a_session": token}
    yield ids
    client.cookies.clear()
    await _wipe()


async def _raw_setting(tid, key):
    async with async_session_factory() as db:
        return (await db.execute(
            sqltext("SELECT value FROM settings_kv WHERE tenant_id = :t AND key = :k"),
            {"t": str(tid), "k": key},
        )).scalar_one_or_none()


async def test_owner_dashboard_cannot_write_instagram_or_connect_google(client, shops) -> None:
    client.cookies.set(auth.SESSION_COOKIE, shops["a_session"])
    for key, value in (("ig_access_token", "EAAstolen0000000000000"), ("ig_user_id", "1784")):
        r = await client.put("/admin/api/settings", json={"key": key, "value": value})
        assert r.status_code == 400, r.text
    for path in ("connect", "callback?code=x&state=y", "sync", "disconnect", "location"):
        r = await client.request("POST" if path in ("sync", "disconnect", "location") else "GET",
                                 f"/admin/api/google-business/{path}")
        assert r.status_code in (404, 405), f"{path} -> {r.status_code}"
    r = await client.get("/admin/api/google-business/status")
    assert r.status_code == 200 and r.json()["connected"] is False


async def test_control_sets_instagram_encrypted_for_that_shop_only(client, shops, monkeypatch) -> None:
    assert secrets.is_enabled(), "TOKEN_ENCRYPTION_KEY must be set (python -m scripts.secure_setup)"

    async def fake_check(user_id, token):
        return "shop_a_insta"
    monkeypatch.setattr(integrations, "check_instagram", fake_check)

    r = await client.put(f"/control/api/tenants/{A}/instagram", headers=CTL,
                         json={"user_id": "17841400000000001", "token": IG_TOKEN})
    assert r.status_code == 200 and r.json()["username"] == "shop_a_insta"

    raw = await _raw_setting(shops["a"], "ig_access_token")
    assert raw["v"].startswith("enc:v1:") and IG_TOKEN not in str(raw), "token stored in plaintext"
    assert await _raw_setting(shops["b"], "ig_access_token") is None

    async with integrations.tenant_db(shops["a"]) as db:
        assert await integrations.instagram_creds(db) == ("17841400000000001", IG_TOKEN)
    async with integrations.tenant_db(shops["b"]) as db:
        assert await integrations.instagram_creds(db) == ("", "")

    r = await client.get(f"/control/api/tenants/{A}/integrations", headers=CTL)
    assert r.json()["instagram"]["linked"] is True and IG_TOKEN not in r.text

    client.cookies.set(auth.SESSION_COOKIE, shops["a_session"])
    r = await client.get("/admin/api/settings")
    assert IG_TOKEN not in r.text and "enc:v1:" not in r.text
    client.cookies.clear()

    r = await client.delete(f"/control/api/tenants/{A}/instagram", headers=CTL)
    assert r.status_code == 200
    async with integrations.tenant_db(shops["a"]) as db:
        assert await integrations.instagram_creds(db) == ("", "")


async def test_control_rejects_bad_instagram_without_saving(client, shops, monkeypatch) -> None:
    async def reject(user_id, token):
        raise integrations.IntegrationError("Meta rejected this Instagram user ID / token")
    monkeypatch.setattr(integrations, "check_instagram", reject)
    r = await client.put(f"/control/api/tenants/{A}/instagram", headers=CTL,
                         json={"user_id": "17841400000000001", "token": IG_TOKEN})
    assert r.status_code == 400
    assert await _raw_setting(shops["a"], "ig_access_token") is None


async def test_google_connect_goes_through_control_and_lands_on_the_right_shop(
    client, shops, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "csecret")

    async def fake_exchange(code, base):
        assert code == "good-code"
        return "refresh-token-for-shop-a"

    async def fake_locations(refresh):
        return [{"account": "accounts/1", "location": "locations/9", "title": "Integ A Laundry"}]

    async def fake_sync(db):
        return {"reviews": []}

    monkeypatch.setattr(gbp, "exchange_code", fake_exchange)
    monkeypatch.setattr(gbp, "list_locations", fake_locations)
    monkeypatch.setattr(gbp, "sync", fake_sync)

    r = await client.get(f"/control/api/tenants/{A}/google-business/connect", headers=CTL,
                         follow_redirects=False)
    assert r.status_code == 302
    assert "control%2Fapi%2Fgoogle-business%2Fcallback" in r.headers["location"]
    state_cookie = r.cookies.get(gbp.STATE_COOKIE)
    state, _, slug = state_cookie.partition(".")
    assert slug == A

    # galat state -> kuch nahi judta, sirf error
    client.cookies.set(gbp.STATE_COOKIE, state_cookie, path="/control/api/google-business")
    r = await client.get("/control/api/google-business/callback?code=good-code&state=wrong",
                         headers=CTL, follow_redirects=False)
    assert r.status_code == 303
    async with integrations.tenant_db(shops["a"]) as db:
        assert not (await gbp.get_connection(db)).get("refresh_token")

    client.cookies.set(gbp.STATE_COOKIE, state_cookie, path="/control/api/google-business")
    r = await client.get(f"/control/api/google-business/callback?code=good-code&state={state}",
                         headers=CTL, follow_redirects=False)
    assert r.status_code == 303
    client.cookies.clear()

    raw = await _raw_setting(shops["a"], "gbp_connection")
    assert raw["v"]["refresh_token"].startswith("enc:v1:")
    assert await _raw_setting(shops["b"], "gbp_connection") is None
    r = await client.get(f"/control/api/tenants/{A}/integrations", headers=CTL)
    g = r.json()["google"]
    assert g["connected"] is True and g["title"] == "Integ A Laundry"
    assert "refresh-token-for-shop-a" not in r.text

    client.cookies.set(auth.SESSION_COOKIE, shops["a_session"])
    r = await client.get("/admin/api/google-business/status")
    assert r.json()["connected"] is True and "refresh-token" not in r.text


def test_secure_setup_fills_missing_keys_and_never_replaces_existing(tmp_path, monkeypatch) -> None:
    from scripts import secure_setup

    env = tmp_path / ".env"
    env.write_text("ADMIN_API_KEY=abc\nVENDOR_API_KEY=abc\nTOKEN_ENCRYPTION_KEY=\n", encoding="utf-8")
    text = env.read_text(encoding="utf-8")
    text = secure_setup._put(text, "TOKEN_ENCRYPTION_KEY", "k1")
    assert secure_setup._get(text, "TOKEN_ENCRYPTION_KEY") == "k1"
    assert text.count("TOKEN_ENCRYPTION_KEY=") == 1
    text = secure_setup._put(text, "APP_DATABASE_URL", "postgresql+asyncpg://kk_app:p@h/db")
    assert secure_setup._get(text, "APP_DATABASE_URL").startswith("postgresql")


async def test_production_refuses_to_start_with_security_gaps(monkeypatch) -> None:
    import app.main as main_mod

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", "")
    with pytest.raises(RuntimeError, match="secure_setup"):
        async with main_mod.lifespan(main_mod.app):
            pass
