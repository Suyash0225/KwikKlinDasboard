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
        async with async_session_factory() as s:
            await s.execute(
                sqltext(
                    "DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users "
                    "WHERE tenant_id IN (SELECT id FROM tenants WHERE owner_phone = :p))"
                ),
                {"p": OUTSIDER_PHONE},
            )
            await s.execute(
                sqltext(
                    "DELETE FROM users WHERE tenant_id IN "
                    "(SELECT id FROM tenants WHERE owner_phone = :p)"
                ),
                {"p": OUTSIDER_PHONE},
            )
            await s.execute(
                sqltext("DELETE FROM tenants WHERE owner_phone = :p"), {"p": OUTSIDER_PHONE}
            )
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
    # server khud bhejta hai — dashboard par NAHI
    assert r.json()["next_url"] == "/welcome"
    assert r.json()["is_home"] is False


async def test_outsider_cannot_read_this_shops_data(client, home_tenant) -> None:
    await _signup_outsider(client)
    for path in (
        "/admin/api/dashboard",
        "/admin/api/customers",
        "/admin/api/inbox/threads",
        "/orders",
    ):
        r = await client.get(path)
        assert r.status_code == 403, f"{path} ne {r.status_code} diya — data leak!"


async def test_outsider_cannot_write_either(client, home_tenant) -> None:
    await _signup_outsider(client)
    r = await client.post(
        "/orders",
        json={"customer_phone": "+919999900011", "items": [{"type": "Shirt", "qty": 1}]},
    )
    assert r.status_code == 403


async def test_outsider_lands_on_their_own_welcome_page(client, home_tenant) -> None:
    await _signup_outsider(client)
    r = await client.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/welcome"


async def test_home_user_gets_in_with_just_a_session(client, home_tenant) -> None:
    """Malik ko ab API key ki zaroorat nahi — uska login hi kaafi hai."""
    async with async_session_factory() as s:
        user = (
            await s.execute(
                select(User).where(
                    User.tenant_id == home_tenant, User.role == ROLE_OWNER
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
