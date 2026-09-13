"""SaaS operations proof: super-admin panel, backups+verify, audit trail,
per-tenant rate limiting.
"""

import pytest
from pathlib import Path
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.services import tenant_context

# /control ka master key. VENDOR_API_KEY set ho to ADMIN_API_KEY wahan
# chalta hi NAHI (orders.vendor_master_key ka jaan-boojh kar rakha gaya
# niyam). Ye test seedha ADMIN_API_KEY bhejte the, isliye purane
# ek-dukaan wale .env par pass hote the aur alag vendor key wale par
# 401. Wahi helper use karo jo server use karta hai — dono soorat mein
# sahi.
from app.routers.orders import vendor_master_key

AUTH = {"X-API-Key": vendor_master_key()}

# Dukaan ka apna key — /orders aur /admin/api ke liye. /control ka master
# key isse alag hota hai (upar AUTH), aur dono ko ek maan lena hi wo
# galti thi jo in tests ko 401 de rahi thi.
SHOP_AUTH = {"X-API-Key": settings.ADMIN_API_KEY}



# ------------------------------------------------------- super-admin panel --

async def test_control_page_serves_shell_without_auth(client) -> None:
    """HTML shell public (zero data usme) — data key ke bina nahi."""
    r = await client.get("/control")
    assert r.status_code == 200 and "KwikKlin Control" in r.text
    assert (await client.get("/control/api/tenants")).status_code == 401


async def test_control_tenants_shows_plans_and_status(client) -> None:
    d = (await client.get("/control/api/tenants", headers=AUTH)).json()
    assert {"total", "active", "trial", "mrr_inr", "arr_inr"} <= set(d["summary"])
    home = next(t for t in d["tenants"] if t["slug"] == "kwik-klin")
    assert home["plan_name"] == "Business" and home["status"] == "active"
    assert "wa_connected" in home and "trial_ends_at" in home


# ------------------------------------------------------------------ audit --

async def test_login_and_failed_login_are_audited(client) -> None:
    await client.post("/api/login", json={"email": "audit-nahi-hai@test.local",
                                          "password": "galat-pass-1"})
    async with async_session_factory() as db:
        row = (
            await db.execute(
                sqltext(
                    "SELECT ok FROM audit_log WHERE action='login_failed' "
                    "AND actor='audit-nahi-hai@test.local' ORDER BY at DESC LIMIT 1"
                )
            )
        ).first()
        await db.execute(
            sqltext("DELETE FROM audit_log WHERE actor='audit-nahi-hai@test.local'")
        )
        await db.commit()
    assert row is not None and row[0] is False


async def test_control_actions_are_audited_and_visible(client) -> None:
    """Tenant change control se -> audit row -> /control/api/audit mein dikhe."""
    r = await client.patch(
        "/control/api/tenants/kwik-klin", headers=AUTH, json={"notes": "audit-test-note"}
    )
    assert r.status_code == 200
    rows = (
        await client.get(
            "/control/api/audit?action=tenant_updated&limit=5", headers=AUTH
        )
    ).json()
    assert any(x["args"].get("tenant") == "kwik-klin" for x in rows)
    # bina key ke audit nahi
    assert (await client.get("/control/api/audit")).status_code == 401
    async with async_session_factory() as db:
        await db.execute(
            sqltext("UPDATE tenants SET notes = NULL WHERE slug='kwik-klin'")
        )
        await db.commit()


# ---------------------------------------------------------------- backups --

async def test_backup_runs_and_restore_verifies() -> None:
    from app.services.backup import run_backup, verify_backup

    path = await run_backup()
    assert path is not None, "backup fail hua"
    assert Path(path).exists() and Path(path).stat().st_size > 10_000
    assert await verify_backup(path) is True

    # kachra file verify FAIL kare — verify sach mein kuch check karta hai
    bad = Path(path).parent / "kwikklin-garbage-test.dump"
    bad.write_bytes(b"ye backup nahi hai")
    try:
        assert await verify_backup(str(bad)) is False
    finally:
        bad.unlink()

    # nightly flow ka audit row bana
    async with async_session_factory() as db:
        row = (
            await db.execute(
                sqltext(
                    "SELECT ok FROM audit_log WHERE action='backup_ok' "
                    "ORDER BY at DESC LIMIT 1"
                )
            )
        ).first()
    assert row is not None and row[0] is True


async def test_control_backup_endpoint(client) -> None:
    r = await client.post("/control/api/backup/run", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert d["verified"] is True and d["file"].endswith(".dump")


# ------------------------------------------------------------ rate limits --

async def test_per_tenant_rate_limit_and_isolation(client, monkeypatch) -> None:
    """6th request 429; DOOSRE tenant ka bucket alag — wo slow nahi hota."""
    import app.main as main_mod
    from app.models.tenant import Tenant, User
    from app.services import auth

    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MIN", 5)
    main_mod._RL_BUCKETS.clear()

    # home tenant: 5 allowed, 6th blocked
    codes = []
    for _ in range(6):
        codes.append((await client.get("/admin/api/rates", headers=SHOP_AUTH)).status_code)
    assert codes[:5] == [200] * 5 and codes[5] == 429

    # tenant B (apna bucket): abhi bhi 200
    async with async_session_factory() as db:
        t = Tenant(slug="test-rl-b", shop_name="RL B", owner_name="B",
                   owner_phone="+919999900095", plan="starter", status="active")
        db.add(t)
        await db.flush()
        u = User(tenant_id=t.id, name="B", email="rl-b@test.local",
                 password_hash=auth.hash_password("rl-pw-12345"), role="OWNER")
        db.add(u)
        await db.commit()
        token = await auth.start_session(db, u, ip="127.0.0.1", user_agent="pytest")
    client.cookies.set("kk_session", token)
    try:
        assert (await client.get("/api/me")).status_code == 200, \
            "tenant B ko home ke traffic ne block kar diya!"
    finally:
        client.cookies.delete("kk_session")
        main_mod._RL_BUCKETS.clear()
        tenant_context.current_tenant_id.set(None)
        async with async_session_factory() as db:
            await db.execute(
                sqltext(
                    "DELETE FROM audit_log WHERE tenant_id IN "
                    "(SELECT id FROM tenants WHERE slug='test-rl-b')"
                )
            )
            await db.execute(
                sqltext(
                    "DELETE FROM login_sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE email='rl-b@test.local')"
                )
            )
            await db.execute(sqltext("DELETE FROM users WHERE email='rl-b@test.local'"))
            await db.execute(sqltext("DELETE FROM tenants WHERE slug='test-rl-b'"))
            await db.commit()


async def test_webhook_and_static_exempt_from_rate_limit(client, monkeypatch) -> None:
    """Meta ke webhook bursts kabhi throttle nahi hone chahiye."""
    import app.main as main_mod

    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MIN", 1)
    main_mod._RL_BUCKETS.clear()
    try:
        for _ in range(3):
            r = await client.get("/webhook", params={
                "hub.mode": "subscribe", "hub.verify_token": "galat", "hub.challenge": "x",
            })
            assert r.status_code == 403  # signature-level reject, 429 NAHI
        assert (await client.get("/health")).status_code == 200
    finally:
        main_mod._RL_BUCKETS.clear()
