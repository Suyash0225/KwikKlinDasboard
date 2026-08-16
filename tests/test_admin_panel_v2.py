"""Admin panel v2 proof: invite-only onboarding, per-admin keys + levels,
soft-delete/restore/purge, dunning ladder, usage-vs-limit, order limits.
"""

from datetime import datetime, timedelta, timezone

import dataclasses
import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models.tenant import TENANT_PAST_DUE, TENANT_TRIAL, Tenant
from app.services import billing, plans, tenant_context

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
SLUG = "test-panel-v2"
PHONE = "+919999900051"
EMAIL = "panel-v2@test.local"


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    tenant_context.current_tenant_id.set(None)
    async with async_session_factory() as db:
        for q, p in [
            ("DELETE FROM billing_events WHERE tenant_id IN (SELECT id FROM tenants WHERE slug LIKE :s)", {"s": "test-panel-%"}),
            ("DELETE FROM invites WHERE tenant_id IN (SELECT id FROM tenants WHERE slug LIKE :s)", {"s": "test-panel-%"}),
            ("DELETE FROM sent_events WHERE event_key LIKE 'dunning:test-panel-%'", None),
            ("DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users WHERE tenant_id IN (SELECT id FROM tenants WHERE slug LIKE :s))", {"s": "test-panel-%"}),
            ("DELETE FROM users WHERE tenant_id IN (SELECT id FROM tenants WHERE slug LIKE :s)", {"s": "test-panel-%"}),
            ("DELETE FROM tenants WHERE slug LIKE :s", {"s": "test-panel-%"}),
            ("DELETE FROM admin_keys WHERE label LIKE 'test-%'", None),
        ]:
            await db.execute(sqltext(q), p or {})
        await db.commit()


async def _create_tenant(client, **over) -> dict:
    body = {
        "shop_name": "Panel V2 Shop", "slug": SLUG, "owner_name": "PV Owner",
        "phone": PHONE, "email": EMAIL, "city": "Varanasi",
        "plan": "premium", "cycle": "annual", "trial_days": 10,
    }
    body.update(over)
    r = await client.post("/control/api/tenants", headers=AUTH, json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------- onboarding --

async def test_add_tenant_slug_validation_and_invite(client) -> None:
    # slug-check live validation
    d = (await client.get("/control/api/slug-check?slug=kwik-klin", headers=AUTH)).json()
    assert d["valid"] is True and d["available"] is False
    d = (await client.get("/control/api/slug-check?slug=Bad_Slug!", headers=AUTH)).json()
    assert d["valid"] is False

    out = await _create_tenant(client)
    assert out["tenant"]["slug"] == SLUG
    assert out["tenant"]["billing_cycle"] == "annual"
    assert "temp_password" not in out
    assert out["invite_path"].startswith("/invite/")

    # duplicate slug / phone -> 409
    r = await client.post("/control/api/tenants", headers=AUTH, json={
        "shop_name": "X2 Shop", "slug": SLUG, "owner_name": "Koi Aur", "phone": "+919999900052",
        "email": "x2@test.local",
    })
    assert r.status_code == 409

    # trial_days custom (10) respect hua
    async with async_session_factory() as db:
        t = (
            await db.execute(sqltext("SELECT trial_ends_at, status FROM tenants WHERE slug=:s"), {"s": SLUG})
        ).first()
    days = (t[0] - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 9.9 < days <= 10.01 and t[1] == TENANT_TRIAL

    # DB mein KAHIN plaintext nahi — user pending sentinel par hai
    async with async_session_factory() as db:
        ph = (
            await db.execute(
                sqltext("SELECT password_hash FROM users WHERE email=:e"), {"e": EMAIL}
            )
        ).scalar_one()
    assert ph == "invite$pending"


async def test_invite_accept_and_reset_flow(client) -> None:
    out = await _create_tenant(client)
    token = out["invite_path"].rsplit("/", 1)[1]
    # chhota password reject
    r = await client.post("/api/invite/accept", json={"token": token, "password": "chhota"})
    assert r.status_code == 422 or r.status_code == 400
    # sahi password -> active + scrypt hash
    r = await client.post("/api/invite/accept", json={"token": token, "password": "panel-pw-123"})
    assert r.status_code == 200
    client.cookies.delete("kk_session")
    async with async_session_factory() as db:
        ph = (
            await db.execute(
                sqltext("SELECT password_hash FROM users WHERE email=:e"), {"e": EMAIL}
            )
        ).scalar_one()
    assert ph.startswith("scrypt$")

    # reset -> naya link, purane sessions khatam; accept se naya password
    r = await client.post(f"/control/api/tenants/{SLUG}/reset-password", headers=AUTH)
    assert r.status_code == 200 and "temp_password" not in r.json()
    rtoken = r.json()["reset_path"].rsplit("/", 1)[1]
    r = await client.post("/api/invite/accept", json={"token": rtoken, "password": "naya-pw-456"})
    assert r.status_code == 200
    client.cookies.delete("kk_session")
    r = await client.post("/api/login", json={"email": EMAIL, "password": "naya-pw-456"})
    assert r.status_code == 200
    client.cookies.delete("kk_session")


# ------------------------------------------------------------ admin keys --

async def test_admin_keys_levels_and_rotation(client) -> None:
    # read key: GET chalta hai, mutation nahi
    r = await client.post("/control/api/admin-keys", headers=AUTH,
                          json={"label": "test-readonly", "level": "read"})
    read_key = r.json()["key"]
    assert read_key.startswith("kk_adm_")
    RK = {"X-API-Key": read_key}
    assert (await client.get("/control/api/tenants", headers=RK)).status_code == 200
    r = await client.patch("/control/api/tenants/kwik-klin", headers=RK, json={"notes": "x"})
    assert r.status_code == 403

    # write key: mutation haan, danger nahi
    r = await client.post("/control/api/admin-keys", headers=AUTH,
                          json={"label": "test-write", "level": "write"})
    WK = {"X-API-Key": r.json()["key"]}
    assert (
        await client.post("/control/api/tenants/kwik-klin/reset-password", headers=WK)
    ).status_code == 403
    assert (
        await client.get("/control/api/admin-keys", headers=WK)
    ).status_code == 403  # keys manage = danger only

    # rotation: revoke ke baad key turant mar jaati hai
    keys = (await client.get("/control/api/admin-keys", headers=AUTH)).json()
    kid = next(k["id"] for k in keys if k["label"] == "test-readonly")
    await client.post(f"/control/api/admin-keys/{kid}/revoke", headers=AUTH)
    assert (await client.get("/control/api/tenants", headers=RK)).status_code == 401


# ------------------------------------------------- lifecycle: soft delete --

async def test_soft_delete_restore_and_purge(client) -> None:
    await _create_tenant(client)
    # bina confirm ke nahi
    assert (
        await client.delete(f"/control/api/tenants/{SLUG}", headers=AUTH)
    ).status_code == 400
    # soft delete -> list se gayab, include_deleted mein dikhta hai
    r = await client.delete(f"/control/api/tenants/{SLUG}?confirm={SLUG}", headers=AUTH)
    assert r.json()["soft"] is True
    slugs = [t["slug"] for t in (await client.get("/control/api/tenants", headers=AUTH)).json()["tenants"]]
    assert SLUG not in slugs
    slugs = [t["slug"] for t in (
        await client.get("/control/api/tenants?include_deleted=true", headers=AUTH)
    ).json()["tenants"]]
    assert SLUG in slugs
    # data zinda hai
    async with async_session_factory() as db:
        assert (
            await db.execute(sqltext("SELECT count(*) FROM tenants WHERE slug=:s"), {"s": SLUG})
        ).scalar_one() == 1
    # restore
    r = await client.post(f"/control/api/tenants/{SLUG}/restore", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == TENANT_PAST_DUE
    # purge sirf soft-deleted par
    r = await client.delete(f"/control/api/tenants/{SLUG}?confirm={SLUG}&purge=true", headers=AUTH)
    assert r.status_code == 400
    await client.delete(f"/control/api/tenants/{SLUG}?confirm={SLUG}", headers=AUTH)
    r = await client.delete(f"/control/api/tenants/{SLUG}?confirm={SLUG}&purge=true", headers=AUTH)
    assert r.status_code == 200 and r.json()["purged"] == SLUG
    async with async_session_factory() as db:
        assert (
            await db.execute(sqltext("SELECT count(*) FROM tenants WHERE slug=:s"), {"s": SLUG})
        ).scalar_one() == 0


# ---------------------------------------------------------------- dunning --

async def test_dunning_reminder_ladder_idempotent(client, monkeypatch) -> None:
    sent: list = []

    from app.services import whatsapp as _wa

    async def _capture(db, *, to_phone, text=None, **kw):
        sent.append({"to": to_phone, "text": text})
        return "wamid.FAKE"

    monkeypatch.setattr(_wa, "send_message", _capture)

    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        db.add(Tenant(
            slug="test-panel-dun", shop_name="Dun Shop", owner_name="D",
            owner_phone="+919999900053", plan="starter", status=TENANT_PAST_DUE,
            trial_ends_at=now - timedelta(days=3, hours=1),
        ))
        await db.commit()

    async with async_session_factory() as db:
        moved = await billing.run_subscription_sweep(db)
    assert moved["reminders"] == 1
    assert sent and "read-only" in sent[0]["text"]

    # replay = koi dusra reminder nahi (sent_events idempotency)
    async with async_session_factory() as db:
        moved = await billing.run_subscription_sweep(db)
    assert moved["reminders"] == 0 and len(sent) == 1

    # billing events feed mein dikha
    rows = (await client.get("/control/api/billing/events?limit=10", headers=AUTH)).json()
    assert any(e["type"] == "dunning_reminder" for e in rows)


# ------------------------------------------------------ limits and usage --

async def test_order_monthly_limit_enforced(client) -> None:
    orig = plans.PLANS["growth"]
    plans.PLANS["growth"] = dataclasses.replace(orig, max_orders_month=0)
    try:
        r = await client.post("/orders", headers=AUTH, json={
            "customer_phone": "+919999900054",
            "items": [{"type": "shirt", "qty": 1}],
        })
        assert r.status_code == 400 and "Upgrade" in r.json()["detail"]
    finally:
        plans.PLANS["growth"] = orig
    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM customers WHERE phone='+919999900054'"))
        await db.commit()


async def test_tenant_list_shows_usage_vs_limit(client) -> None:
    d = (await client.get("/control/api/tenants", headers=AUTH)).json()
    home = next(t for t in d["tenants"] if t["slug"] == "kwik-klin")
    u = home["usage"]
    assert set(u) >= {"orders_month", "orders_limit", "wa_msgs_month",
                      "wa_limit", "staff", "staff_limit"}
    assert u["staff"] >= 1  # asli shop ke staff hain
    assert "trial_warning" in home and home["trial_warning"] is False


async def test_phone_clash_error_names_the_holder_and_recycle_bin(client) -> None:
    """'Pehle se hai' kaafi nahi — 409 bataye number KISKE paas hai, aur
    recycle-bin wala ho to restore/purge ka rasta bhi."""
    out = await _create_tenant(client)
    # live clash -> shop ka naam dikhe
    r = await client.post("/control/api/tenants", headers=AUTH, json={
        "shop_name": "Doosra Shop", "owner_name": "Koi Aur",
        "phone": PHONE, "email": "x9@test.local",
    })
    assert r.status_code == 409 and "Panel V2 Shop" in r.json()["detail"]
    # soft-delete karke phir try -> recycle bin + slug + restore hint
    await client.delete(f"/control/api/tenants/{SLUG}?confirm={SLUG}", headers=AUTH)
    r = await client.post("/control/api/tenants", headers=AUTH, json={
        "shop_name": "Teesra Shop", "owner_name": "Koi Aur",
        "phone": PHONE, "email": "x9@test.local",
    })
    d = r.json()["detail"]
    assert r.status_code == 409
    assert "RECYCLE BIN" in d and SLUG in d and "restore" in d
