"""Client Ops proof: login-as-client, limit overrides, WA-creds via panel,
per-tenant AI toggle — sab audit-logged, sab sirf apne tenant par.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models.tenant import Tenant, User
from app.services import auth, kpis, plans, quota, tenant_context

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
SLUG = "test-ops-b"
EMAIL = "ops-b@test.local"


async def _purge() -> None:
    tenant_context.current_tenant_id.set(None)
    sub = f"(SELECT id FROM tenants WHERE slug = '{SLUG}')"
    async with async_session_factory() as db:
        for q in [
            f"DELETE FROM settings_kv WHERE tenant_id IN {sub}",
            f"DELETE FROM audit_log WHERE tenant_id IN {sub}",
            f"DELETE FROM invites WHERE tenant_id IN {sub}",
            f"DELETE FROM login_sessions WHERE user_id IN "
            f"(SELECT id FROM users WHERE tenant_id IN {sub})",
            f"DELETE FROM users WHERE tenant_id IN {sub}",
            f"DELETE FROM tenants WHERE slug = '{SLUG}'",
        ]:
            await db.execute(sqltext(q))
        await db.commit()
    kpis.invalidate()


@pytest.fixture(autouse=True)
async def _cleanup():
    await _purge()
    yield
    await _purge()


@pytest.fixture
async def tenant_b():
    async with async_session_factory() as db:
        t = Tenant(slug=SLUG, shop_name="Ops B", owner_name="OB",
                   owner_phone="+919999900021", plan="starter", status="active")
        db.add(t)
        await db.flush()
        u = User(tenant_id=t.id, name="OB Owner", email=EMAIL,
                 password_hash=auth.hash_password("ops-pw-12345"), role="OWNER")
        db.add(u)
        await db.commit()
        await db.refresh(t)
        return t


# ------------------------------------------------------------ impersonate --

async def test_impersonate_flow(client, tenant_b) -> None:
    r = await client.post(f"/control/api/tenants/{SLUG}/impersonate", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert d["as_user"] == EMAIL and d["adopt_path"].startswith("/api/session/adopt/")

    # adopt -> cookie set -> /admin redirect -> us tenant ka user
    r = await client.get(d["adopt_path"], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    me = (await client.get("/api/me")).json()
    assert me["user"]["email"] == EMAIL
    client.cookies.delete("kk_session")

    # session chhota hai (30 min), audit hua
    async with async_session_factory() as db:
        mins = (
            await db.execute(sqltext(
                "SELECT EXTRACT(EPOCH FROM (expires_at - now()))/60 FROM login_sessions "
                "WHERE user_id IN (SELECT id FROM users WHERE email=:e) "
                "ORDER BY created_at DESC LIMIT 1"
            ), {"e": EMAIL})
        ).scalar_one()
    assert mins <= 31, f"impersonation session {mins} min ka hai — 30 hona chahiye"
    rows = (
        await client.get(f"/control/api/audit?tenant_slug={SLUG}&action=impersonation",
                         headers=AUTH)
    ).json()
    assert rows and rows[0]["args"]["as_user"] == EMAIL

    # galat token -> login par
    r = await client.get("/api/session/adopt/galat-token", follow_redirects=False)
    assert r.status_code == 303 and "/#login" in r.headers["location"]


async def test_impersonate_needs_danger_key(client, tenant_b) -> None:
    r = await client.post("/control/api/admin-keys", headers=AUTH,
                          json={"label": "test-ops-write", "level": "write"})
    wk = {"X-API-Key": r.json()["key"]}
    assert (
        await client.post(f"/control/api/tenants/{SLUG}/impersonate", headers=wk)
    ).status_code == 403
    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM admin_keys WHERE label='test-ops-write'"))
        await db.commit()


# ------------------------------------------------------- limit overrides --

async def test_limit_overrides_enforced(client, tenant_b) -> None:
    # override: AI 0 (band), WA -1 (unlimited)
    r = await client.put(f"/control/api/tenants/{SLUG}/limits", headers=AUTH,
                         json={"ai_usage_limit": 0, "whatsapp_message_limit": -1})
    assert r.status_code == 200
    eff = r.json()["effective"]
    assert eff["ai_usage_limit"] == 0 and eff["whatsapp_message_limit"] is None

    # enforcement us tenant ke context mein turant
    tok = tenant_context.current_tenant_id.set(tenant_b.id)
    try:
        with pytest.raises(quota.QuotaExceeded):
            await quota.check_ai_quota()
        async with async_session_factory() as db:
            await quota.check_wa_quota(db)  # unlimited — koi exception nahi
    finally:
        tenant_context.current_tenant_id.reset(tok)

    # list mein effective limits dikhte hain
    d = (await client.get(f"/control/api/tenants/{SLUG}", headers=AUTH)).json()
    assert d["usage"]["ai_limit"] == 0 and d["usage"]["wa_limit"] is None
    assert d["limit_overrides"] == {"ai_usage_limit": 0, "whatsapp_message_limit": -1}

    # clear -> plan defaults wapas
    r = await client.put(f"/control/api/tenants/{SLUG}/limits", headers=AUTH,
                         json={"clear": True})
    assert r.json()["effective"]["ai_usage_limit"] == plans.get("starter").ai_usage_limit
    # audit
    rows = (
        await client.get(f"/control/api/audit?tenant_slug={SLUG}&action=limits_overridden",
                         headers=AUTH)
    ).json()
    assert rows


# ------------------------------------------------------------- WA via panel --

async def test_wa_creds_via_panel(client, monkeypatch, tenant_b) -> None:
    from app.services import whatsapp

    async def _fake_validate(pnid, tok):
        return pnid != "badnum"

    monkeypatch.setattr(whatsapp, "validate_credentials", _fake_validate)

    # galat creds -> 400
    r = await client.post(f"/control/api/tenants/{SLUG}/whatsapp", headers=AUTH,
                          json={"phone_number_id": "badnum", "token": "x" * 25})
    assert r.status_code == 400
    # sahi -> save; detail masked (token kabhi wapas nahi)
    r = await client.post(f"/control/api/tenants/{SLUG}/whatsapp", headers=AUTH,
                          json={"phone_number_id": "555000777888",
                                "token": "EAAopsToken00000000000000", "waba_id": "w1"})
    assert r.status_code == 200 and r.json()["connected"] is True
    d = (await client.get(f"/control/api/tenants/{SLUG}", headers=AUTH)).json()
    assert d["wa_connected"] is True
    assert "EAAopsToken" not in str(d)
    # duplicate doosre tenant par -> 409 (kwik-klin ke against nahi — naya slug)
    async with async_session_factory() as db:
        t2 = Tenant(slug="test-ops-c", shop_name="Ops C", owner_name="OC",
                    owner_phone="+919999900022", plan="starter", status="active")
        db.add(t2)
        await db.commit()
    try:
        r = await client.post("/control/api/tenants/test-ops-c/whatsapp", headers=AUTH,
                              json={"phone_number_id": "555000777888", "token": "y" * 25})
        assert r.status_code == 409
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM tenants WHERE slug='test-ops-c'"))
            await db.commit()


# --------------------------------------------------------------- AI toggle --

async def test_agent_toggle_is_per_tenant(client, tenant_b, real_agent_switch) -> None:
    from app.services import app_settings

    r = await client.put(f"/control/api/tenants/{SLUG}/settings", headers=AUTH,
                         json={"key": "agent_enabled", "value": False})
    assert r.status_code == 200 and r.json()["value"] is False

    # B ke ctx mein False; home ki apni value (jo bhi ho) BADLI NAHI
    async with async_session_factory() as db:  # system ctx -> home
        home_before = await app_settings.get(db, "agent_enabled")
    tok = tenant_context.current_tenant_id.set(tenant_b.id)
    try:
        async with async_session_factory() as db:
            assert await app_settings.get(db, "agent_enabled") is False
    finally:
        tenant_context.current_tenant_id.reset(tok)
    async with async_session_factory() as db:
        assert await app_settings.get(db, "agent_enabled") == home_before,             "B ki setting home par lag gayi!"

    # sirf allowed keys
    r = await client.put(f"/control/api/tenants/{SLUG}/settings", headers=AUTH,
                         json={"key": "home_tenant_slug", "value": "hack"})
    assert r.status_code == 400


# ------------------------------------------- agent kill switch = REALLY off --

async def test_agent_off_means_no_autoreply_at_all(client, sent, real_agent_switch) -> None:
    """Owner ne agent band kiya -> bot CHUP. Pehle AI band hone par bhi
    rule-based replies chalti rehti thi (owner ke liye 'band nahi hua')."""
    import json as _json

    from tests.conftest import TEST_CUSTOMER_PHONE, meta_payload, purge_phones, sign_body

    async def _post(text, wamid):
        body = meta_payload(messages=[{
            "from": TEST_CUSTOMER_PHONE.lstrip("+"), "id": wamid,
            "type": "text", "text": {"body": text},
        }])
        return await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
        )

    from app.services import app_settings

    async with async_session_factory() as db:
        was = await app_settings.get(db, "agent_enabled")
        await app_settings.set_value(db, "agent_enabled", True)

    # baseline: agent ON -> customer ko jawab jaata hai
    r = await _post("status batao", "wamid.AGENTSW-1")
    assert r.status_code == 200
    assert len(sent) >= 1, "agent ON par reply jaana chahiye"
    sent.clear()

    # switch OFF (control panel isi setting ko likhta hai)
    async with async_session_factory() as db:
        await app_settings.set_value(db, "agent_enabled", False)
    try:
        r = await _post("status batao", "wamid.AGENTSW-2")
        assert r.status_code == 200
        assert sent == [], f"agent OFF hone par bhi reply gaya: {sent}"
        # message phir bhi store hua (Inbox se owner khud jawab de sakta hai)
        async with async_session_factory() as db:
            n = (
                await db.execute(sqltext(
                    "SELECT count(*) FROM conversations WHERE message_text = 'status batao'"
                ))
            ).scalar_one()
        assert n >= 2, "inbound message store hona hi chahiye"
    finally:
        async with async_session_factory() as db:
            await app_settings.set_value(db, "agent_enabled", was)
        await purge_phones(TEST_CUSTOMER_PHONE)


# ----------------------------------------------- bill delete with a task --

async def test_bill_with_task_can_be_deleted(client, sent) -> None:
    """Pickup/work task order par latka ho to bhi bill delete ho — task ka
    record bacha rehta hai (link toot jaata hai), FK error nahi."""
    from app.models import Task
    from tests.conftest import purge_phones

    phone = "+919999900023"
    r = await client.post("/orders", headers=AUTH, json={
        "customer_phone": phone, "customer_name": "Task Wala",
        "items": [{"type": "shirt", "qty": 1}], "total_amount": "50.00",
    })
    assert r.status_code == 201, r.text
    num = r.json()["order_number"]

    async with async_session_factory() as db:
        oid = (
            await db.execute(
                sqltext("SELECT id FROM orders WHERE order_number = :n"), {"n": num}
            )
        ).scalar_one()
        db.add(Task(code=f"T-{num[-4:]}", title="pickup karo", order_id=oid))
        await db.commit()

    r = await client.delete(f"/orders/{num}", headers=AUTH)
    assert r.status_code == 200, f"delete fail: {r.status_code} {r.text[:150]}"

    async with async_session_factory() as db:
        gone = (
            await db.execute(
                sqltext("SELECT count(*) FROM orders WHERE order_number = :n"), {"n": num}
            )
        ).scalar_one()
        task_left = (
            await db.execute(
                sqltext("SELECT order_id FROM tasks WHERE code = :c"), {"c": f"T-{num[-4:]}"}
            )
        ).scalar_one_or_none()
        await db.execute(sqltext("DELETE FROM tasks WHERE code = :c"), {"c": f"T-{num[-4:]}"})
        await db.commit()
    assert gone == 0, "bill delete nahi hua"
    assert task_left is None, "task ka link null hona chahiye tha"
    await purge_phones(phone)


# ------------------------------------------- password: admin sets it now --

async def test_admin_can_set_owner_password_directly(client, tenant_b) -> None:
    """Support flow: vendor khud naya password set kare. Password DB mein
    sirf HASH jaata hai, purane sessions marte hain, aur user ko pehle
    login par badalna padta hai."""
    async with async_session_factory() as db:
        u = (
            await db.execute(sqltext("SELECT id FROM users WHERE email = :e"), {"e": EMAIL})
        ).scalar_one()
        from app.models.tenant import User as _U
        from app.services import auth as _auth

        user = await db.get(_U, u)
        old_token = await _auth.start_session(db, user, ip="1.1.1.1", user_agent="x")

    r = await client.post(f"/control/api/tenants/{SLUG}/set-password", headers=AUTH,
                          json={"password": "naya-password-123"})
    assert r.status_code == 200
    d = r.json()
    assert d["email"] == EMAIL and d["must_change_password"] is True
    assert "password" not in str(d).lower().replace("must_change_password", "")

    async with async_session_factory() as db:
        row = (
            await db.execute(
                sqltext("SELECT password_hash, must_change_password FROM users WHERE email = :e"),
                {"e": EMAIL},
            )
        ).first()
    assert row[0].startswith("scrypt$"), "plaintext kabhi store nahi hona chahiye"
    assert "naya-password-123" not in row[0]
    assert row[1] is True

    # purana session mar gaya, naya password chalta hai
    async with async_session_factory() as db:
        from app.services import auth as _auth

        assert await _auth.user_for_token(db, old_token) is None
    r = await client.post("/api/login", json={"email": EMAIL, "password": "naya-password-123"})
    assert r.status_code == 200
    client.cookies.delete("kk_session")

    # audit mein action hai, password NAHI
    rows = (
        await client.get(f"/control/api/audit?tenant_slug={SLUG}&action=owner_password_set",
                         headers=AUTH)
    ).json()
    assert rows and "naya-password-123" not in str(rows)

    # chhota password reject
    r = await client.post(f"/control/api/tenants/{SLUG}/set-password", headers=AUTH,
                          json={"password": "chhota"})
    assert r.status_code == 422


async def test_agent_flag_shows_in_tenant_list(client, tenant_b) -> None:
    """Row se hi agent on/off dikhe (panel ka toggle isi par chalta hai)."""
    await client.put(f"/control/api/tenants/{SLUG}/settings", headers=AUTH,
                     json={"key": "agent_enabled", "value": False})
    d = (await client.get(f"/control/api/tenants?q={SLUG}", headers=AUTH)).json()
    row = next(t for t in d["tenants"] if t["slug"] == SLUG)
    assert row["agent_enabled"] is False
    await client.put(f"/control/api/tenants/{SLUG}/settings", headers=AUTH,
                     json={"key": "agent_enabled", "value": True})
    d = (await client.get(f"/control/api/tenants?q={SLUG}", headers=AUTH)).json()
    assert next(t for t in d["tenants"] if t["slug"] == SLUG)["agent_enabled"] is True


# --------------------------------------------------------------- recharge --

async def test_recharge_tops_up_and_is_spent_after_the_limit(client, tenant_b) -> None:
    """Plan limit khatam -> recharge se chalta hai -> recharge khatam ->
    tab jaake band. Har badlaav ledger + audit mein."""
    from app.models.tenant import Tenant

    # limit 0 kar do: har call "limit ke baad" wali ho jaayegi
    await client.put(f"/control/api/tenants/{SLUG}/limits", headers=AUTH,
                     json={"ai_usage_limit": 0})
    # bina recharge -> blocked
    tok = tenant_context.current_tenant_id.set(tenant_b.id)
    try:
        with pytest.raises(quota.QuotaExceeded):
            await quota.check_ai_quota()

        # 2 credits ka recharge
        r = await client.post(f"/control/api/tenants/{SLUG}/credits", headers=AUTH,
                              json={"kind": "ai", "amount": 2, "reason": "diwali offer"})
        assert r.status_code == 200 and r.json()["balance"] == 2

        await quota.check_ai_quota()          # 1st -> credit se chala
        await quota.check_ai_quota()          # 2nd -> credit se chala
        with pytest.raises(quota.QuotaExceeded):
            await quota.check_ai_quota()      # credits khatam
    finally:
        tenant_context.current_tenant_id.reset(tok)

    async with async_session_factory() as db:
        t = await db.get(Tenant, tenant_b.id)
        assert t.ai_credits == 0, "credits kharch hone chahiye the"

    # ledger + balance history
    h = (await client.get(f"/control/api/tenants/{SLUG}/credits", headers=AUTH)).json()
    assert h["ai_credits"] == 0
    assert h["history"] and h["history"][0]["amount"] == 2
    assert h["history"][0]["reason"] == "diwali offer"

    # wapas lena bhi chalta hai, balance kabhi minus nahi
    await client.post(f"/control/api/tenants/{SLUG}/credits", headers=AUTH,
                      json={"kind": "wa", "amount": 50})
    r = await client.post(f"/control/api/tenants/{SLUG}/credits", headers=AUTH,
                          json={"kind": "wa", "amount": -500})
    assert r.json()["balance"] == 0

    # audit trail
    rows = (
        await client.get(f"/control/api/audit?tenant_slug={SLUG}&action=credits_recharged",
                         headers=AUTH)
    ).json()
    assert len(rows) >= 2

    # list/detail mein balances dikhte hain
    d = (await client.get(f"/control/api/tenants/{SLUG}", headers=AUTH)).json()
    assert d["credits"] == {"ai": 0, "wa": 0}
    await client.put(f"/control/api/tenants/{SLUG}/limits", headers=AUTH, json={"clear": True})


async def test_recharge_rejects_zero_and_bad_kind(client, tenant_b) -> None:
    assert (await client.post(f"/control/api/tenants/{SLUG}/credits", headers=AUTH,
                              json={"kind": "ai", "amount": 0})).status_code == 400
    assert (await client.post(f"/control/api/tenants/{SLUG}/credits", headers=AUTH,
                              json={"kind": "gold", "amount": 5})).status_code == 422


async def test_recharge_validity_expires_leftover_only(client, tenant_b) -> None:
    """Validity wala top-up: bacha hua hissa hi expire hota hai, kharch
    kiya hua wapas nahi hota, aur ledger mein poora trail rehta hai."""
    from datetime import datetime, timedelta, timezone

    from app.models import CreditLedger
    from app.models.tenant import Tenant
    from app.services.credits import expire_due_credits

    # 10 WA credits, 7 din ki validity
    r = await client.post(f"/control/api/tenants/{SLUG}/credits", headers=AUTH,
                          json={"kind": "wa", "amount": 10, "valid_days": 7,
                                "reason": "GPay 4283 · ₹100"})
    assert r.status_code == 200 and r.json()["expires_at"] is not None

    # 4 kharch ho gaye (limit 0 -> har send credit se)
    await client.put(f"/control/api/tenants/{SLUG}/limits", headers=AUTH,
                     json={"whatsapp_message_limit": 0})
    tok = tenant_context.current_tenant_id.set(tenant_b.id)
    try:
        async with async_session_factory() as db:
            for _ in range(4):
                await quota.check_wa_quota(db)
    finally:
        tenant_context.current_tenant_id.reset(tok)

    # validity guzar gayi
    async with async_session_factory() as db:
        lot = (
            await db.execute(
                sqltext("SELECT id FROM credit_ledger WHERE tenant_id = :t "
                        "AND amount = 10 ORDER BY at DESC LIMIT 1"),
                {"t": str(tenant_b.id)},
            )
        ).scalar_one()
        await db.execute(
            sqltext("UPDATE credit_ledger SET expires_at = :e WHERE id = :i"),
            {"e": datetime.now(timezone.utc) - timedelta(minutes=1), "i": lot},
        )
        await db.commit()

    async with async_session_factory() as db:
        cut = await expire_due_credits(db)
    assert cut["wa"] == 6, f"sirf bacha hua (10-4) expire hona chahiye, hua {cut}"

    async with async_session_factory() as db:
        t = await db.get(Tenant, tenant_b.id)
        assert t.wa_credits == 0
        rows = (
            await db.execute(
                sqltext("SELECT amount, reason FROM credit_ledger WHERE tenant_id = :t "
                        "ORDER BY at DESC LIMIT 1"),
                {"t": str(tenant_b.id)},
            )
        ).first()
    assert rows[0] == -6 and "expired" in rows[1]

    # dobara chalao -> kuch nahi kaatta (lot process ho chuka)
    async with async_session_factory() as db:
        assert await expire_due_credits(db) == {"ai": 0, "wa": 0}
    await client.put(f"/control/api/tenants/{SLUG}/limits", headers=AUTH, json={"clear": True})


async def test_recharge_without_validity_never_expires(client, tenant_b) -> None:
    from app.services.credits import expire_due_credits

    await client.post(f"/control/api/tenants/{SLUG}/credits", headers=AUTH,
                      json={"kind": "ai", "amount": 25})
    async with async_session_factory() as db:
        assert await expire_due_credits(db) == {"ai": 0, "wa": 0}
    h = (await client.get(f"/control/api/tenants/{SLUG}/credits", headers=AUTH)).json()
    assert h["ai_credits"] == 25 and h["history"][0]["expires_at"] is None
