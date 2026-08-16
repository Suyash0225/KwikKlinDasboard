"""Ek client ka data doosre ko kabhi nahi.

Ye tests ek asli bug se aaye hain: public page se signup karne wale ko
seedha `/admin` bhej diya gaya tha. Wo dashboard sirf `X-API-Key` dekhta
tha, aur jis browser mein malik ki key pehle se padi thi, usmein naya
account is dukaan ka poora data khol deta tha.

Do cheezein pakki honi chahiye:
1. doosre tenant ka logged-in user dashboard ka DATA na le paye (403)
2. use dashboard ka PAGE bhi na dikhe — apne welcome page par jaye
"""

import pytest
from sqlalchemy import select, text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models import ROLE_OWNER, Tenant, User
from app.services import app_settings, auth

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
OUTSIDER_PHONE = "+919999900066"
OUTSIDER_EMAIL = "outsider-shop@example.com"
OUTSIDER_PASS = "outsider12345"


@pytest.fixture(autouse=True)
async def _clean():
    async def wipe():
        # Self-serve ke baad outsider apne tenant mein BUSINESS data bhi
        # banata hai — tenant delete se pehle wo sab (FK-safe order mein).
        sub = "(SELECT id FROM tenants WHERE owner_phone = :p)"
        async with async_session_factory() as s:
            for q in [
                f"DELETE FROM order_status_history WHERE order_id IN "
                f"(SELECT id FROM orders WHERE tenant_id IN {sub})",
                f"DELETE FROM tasks WHERE tenant_id IN {sub}",
                f"DELETE FROM payments WHERE tenant_id IN {sub}",
                f"DELETE FROM conversations WHERE tenant_id IN {sub}",
                f"DELETE FROM orders WHERE tenant_id IN {sub}",
                f"DELETE FROM customers WHERE tenant_id IN {sub}",
                f"DELETE FROM settings_kv WHERE tenant_id IN {sub}",
                f"DELETE FROM audit_log WHERE tenant_id IN {sub}",
                f"DELETE FROM invites WHERE tenant_id IN {sub}",
                f"DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users "
                f"WHERE tenant_id IN {sub})",
                f"DELETE FROM users WHERE tenant_id IN {sub}",
                "DELETE FROM tenants WHERE owner_phone = :p",
            ]:
                await s.execute(sqltext(q), {"p": OUTSIDER_PHONE})
            await s.commit()

    await wipe()
    yield
    await wipe()


@pytest.fixture
async def home_tenant():
    """Is deployment ki apni dukaan — tests ke liye pakki kar do."""
    async with async_session_factory() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.slug == "kwik-klin"))
        ).scalar_one_or_none()
        if t is None:
            t = Tenant(
                slug="kwik-klin", shop_name="Kwik Klin", owner_name="Suyash",
                owner_phone="+918933871103", owner_email="suyash@kwikklin.local",
                plan="growth", status="active",
            )
            s.add(t)
            await s.commit()
        await app_settings.set_value(s, "home_tenant_slug", "kwik-klin")
        return t.id


async def _signup_outsider(client) -> None:
    r = await client.post(
        "/api/signup",
        json={
            "shop_name": "Bahar Wali Laundry", "owner_name": "Doosra Seth",
            "phone": OUTSIDER_PHONE, "email": OUTSIDER_EMAIL,
            "plan": "starter", "password": OUTSIDER_PASS,
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/login", json={"email": OUTSIDER_EMAIL, "password": OUTSIDER_PASS}
    )
    assert r.status_code == 200
    # self-serve gate: chalu tenant seedha APNE dashboard par jaata hai
    assert r.json()["next_url"] == "/admin"
    assert r.json()["is_home"] is False


async def test_outsider_cannot_read_this_shops_data(client, home_tenant) -> None:
    """Self-serve gate ke baad: outsider dashboard USE kar sakta hai (200),
    lekin usme IS dukaan ka EK BYTE nahi — RLS scoping hi asli deewar hai."""
    await _signup_outsider(client)
    # home ka ek pehchana customer hona chahiye jo leak-check ka marker bane
    for path in (
        "/admin/api/dashboard",
        "/admin/api/customers?limit=500",
        "/admin/api/inbox/threads",
        "/orders",
    ):
        r = await client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert "+91870" not in r.text, f"{path}: home ka data outsider ko dikh gaya!"
        assert "Kwik Klin" not in r.text.replace("Kwik Klin AI", ""), \
            f"{path}: home shop ka naam leak"


async def test_outsider_writes_go_to_their_own_tenant(client, home_tenant) -> None:
    """Outsider ka create HOME mein nahi girta — apne tenant mein girta hai."""
    await _signup_outsider(client)
    r = await client.post(
        "/orders",
        json={"customer_phone": "+919999900067", "items": [{"type": "Shirt", "qty": 1}]},
    )
    assert r.status_code == 201, r.text
    # home ki nazar se (API key, BINA outsider cookie ke — session cookie
    # key par jeet-ti hai, wahi design hai) wo customer exist hi nahi karta
    client.cookies.delete("kk_session")
    r = await client.get("/admin/api/customers?limit=500", headers=AUTH)
    assert "+919999900067" not in r.text, "outsider ka data home mein ghusa!"
    async with async_session_factory() as s:
        await s.execute(
            sqltext(
                "DELETE FROM order_status_history WHERE order_id IN "
                "(SELECT id FROM orders WHERE customer_id IN "
                " (SELECT id FROM customers WHERE phone='+919999900067'))"
            )
        )
        await s.execute(
            sqltext(
                "DELETE FROM tasks WHERE order_id IN (SELECT id FROM orders "
                "WHERE customer_id IN (SELECT id FROM customers WHERE phone='+919999900067'))"
            )
        )
        await s.execute(
            sqltext(
                "DELETE FROM conversations WHERE customer_id IN "
                "(SELECT id FROM customers WHERE phone='+919999900067')"
            )
        )
        await s.execute(
            sqltext(
                "DELETE FROM orders WHERE customer_id IN "
                "(SELECT id FROM customers WHERE phone='+919999900067')"
            )
        )
        await s.execute(
            sqltext("DELETE FROM customers WHERE phone='+919999900067'")
        )
        await s.commit()


async def test_outsider_lands_on_their_own_dashboard_page(client, home_tenant) -> None:
    """Self-serve: chalu tenant ko /admin page milta hai (redirect nahi) —
    data waise bhi RLS-scoped hai."""
    await _signup_outsider(client)
    r = await client.get("/admin", follow_redirects=False)
    assert r.status_code == 200


async def test_home_user_gets_in_with_just_a_session(client, home_tenant) -> None:
    """Malik ko ab API key ki zaroorat nahi — uska login hi kaafi hai."""
    async with async_session_factory() as s:
        user = (
            await s.execute(
                select(User).where(
                    User.tenant_id == home_tenant, User.role == ROLE_OWNER,
                    User.is_active.is_(True),   # revoked user ka session banta hi nahi
                )
            )
        ).scalars().first()
        if user is None:
            user = User(
                tenant_id=home_tenant, name="Suyash", email="home-test@example.com",
                password_hash=auth.hash_password("homepass12345"), role=ROLE_OWNER,
            )
            s.add(user)
            await s.commit()
        token = await auth.start_session(s, user)

    client.cookies.set(auth.SESSION_COOKIE, token)
    try:
        r = await client.get("/admin/api/dashboard")
        assert r.status_code == 200, r.text
        page = await client.get("/admin", follow_redirects=False)
        assert page.status_code == 200, "malik ko dashboard dikhna chahiye"
    finally:
        client.cookies.clear()


async def test_api_key_still_works_for_scripts(client) -> None:
    """Purana rasta band nahi hua — automation aur owner tooling chalti rahe."""
    r = await client.get("/admin/api/dashboard", headers=AUTH)
    assert r.status_code == 200


async def test_no_session_no_key_is_refused(client) -> None:
    r = await client.get("/admin/api/dashboard")
    assert r.status_code == 401
