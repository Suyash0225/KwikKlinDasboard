"""Multi-tenant authentication & role enforcement proof.

Kya prove karte hain:
1. HOME tenant ka STAFF user: operational endpoints chalte hain (dashboard,
   orders, inbox, rates read), lekin paisa/settings/staff-mgmt/exports 403.
2. OWNER session: wahi sab 200.
3. Control panel: session se KOI nahi ghus sakta (OWNER bhi) — sirf API key.
4. Logout ab server par session sach mein revoke karta hai (purana token
   dobara kaam nahi karta).
5. Ek hi email do tenants mein: password decide karta hai kaun sa account
   milta hai — dono deterministically apne apne tenant mein utarte hain.
"""

import uuid

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models.tenant import Tenant, User
from app.services import auth, tenant_context

# /control ka master key. VENDOR_API_KEY set ho to ADMIN_API_KEY wahan
# chalta hi NAHI (orders.vendor_master_key ka jaan-boojh kar rakha gaya
# niyam). Ye test seedha ADMIN_API_KEY bhejte the, isliye purane
# ek-dukaan wale .env par pass hote the aur alag vendor key wale par
# 401. Wahi helper use karo jo server use karta hai — dono soorat mein
# sahi.
from app.routers.orders import vendor_master_key

AUTH = {"X-API-Key": vendor_master_key()}

STAFF_EMAIL = "roles-staff@test.local"
OWNER_EMAIL = "roles-owner@test.local"
DUP_EMAIL = "roles-dup@test.local"
C_SLUG = "test-roles-c"

OWNER_ONLY_GETS = [
    "/admin/api/expenses",
    "/admin/api/reports/summary",
    "/admin/api/settings",       # agent_admin router
    "/admin/api/usage",          # agent_admin router
    "/admin/api/export/orders.csv",
]
STAFF_ALLOWED_GETS = [
    "/admin/api/dashboard",
    "/orders",
    "/admin/api/rates",
    "/admin/api/inbox/threads",
    "/admin/api/staff",
]


async def _make_user(email: str, role: str, password: str, tenant_id=None) -> str:
    """User banao (home tenant by default) aur uska session token do."""
    if tenant_id is None:
        tenant_id = await tenant_context.get_home_tenant_id()
    async with async_session_factory() as db:
        u = User(
            tenant_id=tenant_id,
            name=f"RoleTest {role}",
            email=email,
            password_hash=auth.hash_password(password),
            role=role,
        )
        db.add(u)
        await db.commit()
        return await auth.start_session(db, u, ip="127.0.0.1", user_agent="pytest")


@pytest.fixture
async def role_users():
    staff_token = await _make_user(STAFF_EMAIL, "STAFF", "staff-pw-123")
    owner_token = await _make_user(OWNER_EMAIL, "OWNER", "owner-pw-123")
    try:
        yield staff_token, owner_token
    finally:
        tenant_context.current_tenant_id.set(None)
        async with async_session_factory() as db:
            await db.execute(
                sqltext(
                    "DELETE FROM login_sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE email IN (:a, :b, :c))"
                ),
                {"a": STAFF_EMAIL, "b": OWNER_EMAIL, "c": DUP_EMAIL},
            )
            await db.execute(
                sqltext("DELETE FROM users WHERE email IN (:a, :b, :c)"),
                {"a": STAFF_EMAIL, "b": OWNER_EMAIL, "c": DUP_EMAIL},
            )
            await db.execute(
                sqltext("DELETE FROM tenants WHERE slug = :s"), {"s": C_SLUG}
            )
            await db.commit()


async def test_staff_operational_access_works(client, role_users) -> None:
    staff_token, _ = role_users
    client.cookies.set("kk_session", staff_token)
    try:
        for path in STAFF_ALLOWED_GETS:
            r = await client.get(path)
            assert r.status_code == 200, f"STAFF blocked on {path}: {r.status_code}"
    finally:
        client.cookies.delete("kk_session")


async def test_staff_blocked_from_owner_endpoints(client, role_users) -> None:
    staff_token, _ = role_users
    client.cookies.set("kk_session", staff_token)
    try:
        for path in OWNER_ONLY_GETS:
            r = await client.get(path)
            assert r.status_code == 403, f"STAFF got {r.status_code} on {path}!"
        # Writes bhi: staff member add nahi kar sakta, bill delete nahi
        r = await client.post(
            "/admin/api/staff", json={"name": "X", "phone": "+919999900097", "role": "WASHER"}
        )
        assert r.status_code == 403
        r = await client.delete("/orders/KK-00000000-99")
        assert r.status_code == 403  # dependency 404 se pehle chalti hai
    finally:
        client.cookies.delete("kk_session")


async def test_owner_session_has_full_dashboard_access(client, role_users) -> None:
    _, owner_token = role_users
    client.cookies.set("kk_session", owner_token)
    try:
        for path in OWNER_ONLY_GETS:
            r = await client.get(path)
            assert r.status_code == 200, f"OWNER blocked on {path}: {r.status_code}"
    finally:
        client.cookies.delete("kk_session")


async def test_control_panel_is_key_only(client, role_users) -> None:
    """Session se koi nahi — home OWNER bhi nahi. Sirf X-API-Key."""
    _, owner_token = role_users
    client.cookies.set("kk_session", owner_token)
    try:
        r = await client.get("/control/api/tenants")
        assert r.status_code == 401, f"OWNER session reached /control: {r.status_code}"
    finally:
        client.cookies.delete("kk_session")
    r = await client.get("/control/api/tenants", headers=AUTH)
    assert r.status_code == 200


async def test_logout_revokes_session_server_side(client, role_users) -> None:
    staff_token, _ = role_users
    client.cookies.set("kk_session", staff_token)
    try:
        assert (await client.get("/api/me")).status_code == 200
        r = await client.post("/api/logout")
        assert r.status_code == 200
        # Token chura bhi liya ho to ab bekaar hai — server side revoked.
        client.cookies.set("kk_session", staff_token)
        assert (await client.get("/api/me")).status_code == 401
    finally:
        client.cookies.delete("kk_session")


async def test_same_email_two_tenants_login_is_deterministic(client) -> None:
    home_id = await tenant_context.get_home_tenant_id()
    async with async_session_factory() as db:
        tenant_c = Tenant(
            slug=C_SLUG, shop_name="Roles Test C", owner_name="C Owner",
            owner_phone="+919999900096", plan="starter", status="active",
        )
        db.add(tenant_c)
        await db.flush()
        c_id = tenant_c.id
        db.add(User(tenant_id=home_id, name="Dup Home", email=DUP_EMAIL,
                    password_hash=auth.hash_password("home-pw-123"), role="OWNER"))
        db.add(User(tenant_id=c_id, name="Dup C", email=DUP_EMAIL,
                    password_hash=auth.hash_password("cccc-pw-123"), role="OWNER"))
        await db.commit()
    try:
        r = await client.post("/api/login", json={"email": DUP_EMAIL, "password": "home-pw-123"})
        assert r.status_code == 200 and r.json()["is_home"] is True
        client.cookies.delete("kk_session")
        r = await client.post("/api/login", json={"email": DUP_EMAIL, "password": "cccc-pw-123"})
        assert r.status_code == 200
        body = r.json()
        assert body["is_home"] is False
        assert body["tenant"]["shop_name"] == "Roles Test C"
        client.cookies.delete("kk_session")
        r = await client.post("/api/login", json={"email": DUP_EMAIL, "password": "galat-pw"})
        assert r.status_code == 401
    finally:
        client.cookies.delete("kk_session")
        tenant_context.current_tenant_id.set(None)
        async with async_session_factory() as db:
            await db.execute(
                sqltext(
                    "DELETE FROM login_sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE email = :e)"
                ),
                {"e": DUP_EMAIL},
            )
            await db.execute(sqltext("DELETE FROM users WHERE email = :e"), {"e": DUP_EMAIL})
            await db.execute(sqltext("DELETE FROM tenants WHERE slug = :s"), {"s": C_SLUG})
            await db.commit()
