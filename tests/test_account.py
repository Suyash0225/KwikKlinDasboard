"""Bechne ka rasta: signup -> login -> plan limits -> control panel.

Ye wo layer hai jisse software bikta hai, isliye yahan galti mehngi hai:
ek galat 200 ka matlab hai kisi ne bina paise ke ghus liya, ya kisi client
ka data doosre ko dikh gaya.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models import (
    ROLE_OWNER,
    TENANT_ACTIVE,
    TENANT_READ_ONLY,
    TENANT_TRIAL,
    Tenant,
    User,
)
from app.services import auth, billing, plans

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
PHONE = "+919999900077"
PHONE2 = "+919999900078"
EMAIL = "shop-test@example.com"
PASSWORD = "laundry12345"


@pytest.fixture(autouse=True)
async def _clean():
    async def wipe():
        async with async_session_factory() as s:
            await s.execute(
                sqltext(
                    "DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users "
                    "WHERE tenant_id IN (SELECT id FROM tenants WHERE owner_phone IN "
                    f"('{PHONE}','{PHONE2}')))"
                )
            )
            await s.execute(
                sqltext(
                    "DELETE FROM users WHERE tenant_id IN (SELECT id FROM tenants "
                    f"WHERE owner_phone IN ('{PHONE}','{PHONE2}'))"
                )
            )
            await s.execute(
                sqltext(
                    "DELETE FROM billing_events WHERE tenant_id IN (SELECT id FROM tenants "
                    f"WHERE owner_phone IN ('{PHONE}','{PHONE2}'))"
                )
            )
            await s.execute(
                sqltext(f"DELETE FROM tenants WHERE owner_phone IN ('{PHONE}','{PHONE2}')")
            )
            await s.commit()

    await wipe()
    yield
    await wipe()


async def _signup(client, **over) -> dict:
    body = {
        "shop_name": "Test Laundry", "owner_name": "Test Owner", "phone": PHONE,
        "email": EMAIL, "city": "Varanasi", "plan": "starter", "password": PASSWORD,
    }
    body.update(over)
    r = await client.post("/api/signup", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _login(client, email=EMAIL, password=PASSWORD):
    return await client.post("/api/login", json={"email": email, "password": password})


# --- signup -----------------------------------------------------------------


async def test_signup_creates_a_trial_shop_and_an_owner(client) -> None:
    out = await _signup(client)
    assert out["tenant"]["plan"] == "starter"
    async with async_session_factory() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one()
        u = (await s.execute(select(User).where(User.tenant_id == t.id))).scalar_one()
    assert t.status == TENANT_TRIAL and t.trial_ends_at is not None
    assert u.role == ROLE_OWNER and u.email == EMAIL
    assert PASSWORD not in u.password_hash, "password kabhi plain text mein nahi"


async def test_same_number_cannot_sign_up_twice(client) -> None:
    await _signup(client)
    r = await client.post(
        "/api/signup",
        json={"shop_name": "Dusri", "owner_name": "Koi Aur", "phone": PHONE,
              "email": "x@example.com", "password": PASSWORD},
    )
    assert r.status_code == 409


async def test_weak_password_is_refused(client) -> None:
    r = await client.post(
        "/api/signup",
        json={"shop_name": "S", "owner_name": "O", "phone": PHONE,
              "email": EMAIL, "password": "123"},
    )
    assert r.status_code == 422  # pydantic min_length


# --- login ------------------------------------------------------------------


async def test_login_sets_a_session_and_me_works(client) -> None:
    await _signup(client)
    r = await _login(client)
    assert r.status_code == 200
    assert auth.SESSION_COOKIE in r.cookies or "set-cookie" in r.headers

    me = await client.get("/api/me")
    assert me.status_code == 200, me.text
    d = me.json()
    assert d["user"]["role"] == ROLE_OWNER
    assert d["tenant"]["plan"] == "starter"
    assert d["tenant"]["limits"]["orders_month"] == 400
    assert d["can_write"] is True


async def test_wrong_password_is_rejected_and_says_nothing_useful(client) -> None:
    await _signup(client)
    r = await _login(client, password="galatpassword")
    assert r.status_code == 401
    # unknown email must look EXACTLY the same — no user enumeration
    r2 = await _login(client, email="nobody@example.com", password=PASSWORD)
    assert r2.status_code == 401
    assert r.json()["detail"] == r2.json()["detail"]


async def test_no_session_no_entry(client) -> None:
    assert (await client.get("/api/me")).status_code == 401


async def test_logout_kills_the_session(client) -> None:
    await _signup(client)
    await _login(client)
    assert (await client.get("/api/me")).status_code == 200
    await client.post("/api/logout")
    client.cookies.clear()
    assert (await client.get("/api/me")).status_code == 401


async def test_password_change_logs_every_device_out(client) -> None:
    await _signup(client)
    await _login(client)
    r = await client.post(
        "/api/me/password",
        json={"current_password": PASSWORD, "new_password": "naya-password-123"},
    )
    assert r.status_code == 200
    async with async_session_factory() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one()
        u = (await s.execute(select(User).where(User.tenant_id == t.id))).scalar_one()
        n = (
            await s.execute(
                sqltext("SELECT count(*) FROM login_sessions WHERE user_id = :u"),
                {"u": str(u.id)},
            )
        ).scalar_one()
    assert auth.verify_password("naya-password-123", u.password_hash)
    assert n == 1, "purani sab sessions band, sirf yahi bachni chahiye"


# --- plan limits ------------------------------------------------------------


async def test_staff_limit_is_enforced_with_an_upgrade_hint(client) -> None:
    await _signup(client)
    await _login(client)
    # Starter = 3 users, owner pehle se ek hai
    for i in range(2):
        r = await client.post(
            "/api/users",
            json={"name": f"Kaam Wala {i}", "email": f"k{i}@example.com", "role": "MANAGER"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["temp_password"], "naye user ko password milna chahiye"
    r = await client.post(
        "/api/users", json={"name": "Chautha", "email": "c@example.com", "role": "STAFF"}
    )
    assert r.status_code == 402, "limit par 402 aana chahiye, 500 nahi"
    assert "Pro" in r.json()["detail"]


async def test_read_only_tenant_can_look_but_not_touch(client) -> None:
    """Paisa ruka: data dikhta hai, badalta nahi. Delete kabhi nahi."""
    await _signup(client)
    await _login(client)
    async with async_session_factory() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one()
        t.status = TENANT_READ_ONLY
        await s.commit()

    me = await client.get("/api/me")
    assert me.status_code == 200 and me.json()["can_write"] is False

    r = await client.post(
        "/api/users", json={"name": "Naya", "email": "n@example.com", "role": "STAFF"}
    )
    assert r.status_code == 402


# --- billing ----------------------------------------------------------------


def test_price_breakup_has_gst_and_setup_fee() -> None:
    b = billing.price_breakup("starter", annual=False, with_setup=True)
    assert b["plan_inr"] == 999 and b["setup_inr"] == plans.SETUP_FEE_INR
    assert b["gst_inr"] == round((999 + plans.SETUP_FEE_INR) * 18 / 100)
    assert b["total_paise"] == b["total_inr"] * 100

    b2 = billing.price_breakup("pro", annual=True, with_setup=False)
    assert b2["plan_inr"] == plans.PLANS["pro"].annual_inr and b2["setup_inr"] == 0


async def test_webhook_without_a_valid_signature_activates_nobody(client) -> None:
    await _signup(client)
    r = await client.post(
        "/webhooks/razorpay",
        json={"event": "payment.captured", "payload": {}},
        headers={"X-Razorpay-Signature": "nakli"},
    )
    assert r.status_code == 403
    async with async_session_factory() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one()
    assert t.status == TENANT_TRIAL, "bina signature ke koi activate nahi hoga"


async def test_paid_webhook_activates_the_shop_and_replay_is_safe(client) -> None:
    await _signup(client)
    async with async_session_factory() as db:
        t = (
            await db.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one()
        event = {
            "event": "payment.captured",
            "id": "evt_test_001",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_test_001", "amount": 353600,
                        "notes": {"tenant_slug": t.slug, "plan": "pro", "cycle": "monthly"},
                    }
                }
            },
        }
        assert await billing.handle_event(db, event) == "activated"
        # Razorpay retries — dobara chalu/charge nahi hona chahiye
        assert await billing.handle_event(db, event) == "duplicate"

    async with async_session_factory() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one()
    assert t.status == TENANT_ACTIVE
    assert t.plan == "pro" and t.setup_fee_paid is True
    assert t.current_period_end > datetime.now(timezone.utc)


async def test_dunning_moves_a_stale_past_due_to_read_only(client) -> None:
    await _signup(client)
    async with async_session_factory() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one()
        t.status = "past_due"
        t.current_period_end = datetime.now(timezone.utc) - timedelta(days=10)
        await s.commit()
    async with async_session_factory() as db:
        assert await billing.run_dunning(db) >= 1
    async with async_session_factory() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one()
    assert t.status == TENANT_READ_ONLY


# --- control panel (Suyash) -------------------------------------------------


async def test_control_panel_needs_the_admin_key(client) -> None:
    assert (await client.get("/control/api/tenants")).status_code == 401


async def test_control_panel_lists_clients_with_mrr(client) -> None:
    await _signup(client)
    r = await client.get("/control/api/tenants", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    mine = [t for t in d["tenants"] if t["owner_phone"] == PHONE]
    assert mine and mine[0]["plan"] == "starter"
    assert d["summary"]["total"] >= 1
    assert "mrr_inr" in d["summary"] and "onboarding_pending" in d["summary"]


async def test_owner_can_create_a_client_and_hand_over_a_password(client) -> None:
    r = await client.post(
        "/control/api/tenants", headers=AUTH,
        json={"shop_name": "Phone Par Bika", "owner_name": "Seth ji", "phone": PHONE2,
              "email": "seth@example.com", "plan": "pro", "city": "Varanasi"},
    )
    assert r.status_code == 201, r.text
    temp = r.json()["temp_password"]
    assert temp

    # aur wo password sach mein chalta hai
    login = await _login(client, email="seth@example.com", password=temp)
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True


async def test_duplicate_signup_names_the_shop_that_has_the_number(client) -> None:
    """'Number pehle se hai' se kisi ko kuch samajh nahi aata — batao kiska."""
    await _signup(client)
    r = await client.post(
        "/api/signup",
        json={"shop_name": "Doosri Dukaan", "owner_name": "Koi Aur", "phone": PHONE,
              "email": "aur@example.com", "password": PASSWORD},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "Test Laundry" in detail and "Login" in detail


async def test_a_customers_phone_does_not_block_signup(client) -> None:
    """Is dukaan ka customer khud bhi laundry chala sakta hai — uska number
    tenants ke namespace mein rukawat nahi hai."""
    from app.models import Customer

    async with async_session_factory() as s:
        s.add(Customer(phone=PHONE2, name="Grahak Jo Dukaandaar Bhi Hai"))
        await s.commit()
    try:
        r = await client.post(
            "/api/signup",
            json={"shop_name": "Grahak Ki Laundry", "owner_name": "Grahak Seth",
                  "phone": PHONE2, "email": "grahak@example.com", "password": PASSWORD},
        )
        assert r.status_code == 201, r.text
    finally:
        async with async_session_factory() as s:
            await s.execute(sqltext(f"DELETE FROM customers WHERE phone = '{PHONE2}'"))
            await s.commit()


async def test_test_signups_can_be_deleted_but_home_never(client) -> None:
    await _signup(client)
    async with async_session_factory() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one()
        slug = t.slug

    # bina confirm ke kuch nahi hota
    assert (
        await client.delete(f"/control/api/tenants/{slug}", headers=AUTH)
    ).status_code == 400

    r = await client.delete(
        f"/control/api/tenants/{slug}?confirm={slug}", headers=AUTH
    )
    assert r.status_code == 200 and r.json()["users_removed"] >= 1
    async with async_session_factory() as s:
        gone = (
            await s.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one_or_none()
    assert gone is None

    # apni dukaan kabhi delete nahi hoti
    from app.services import auth as auth_service

    async with async_session_factory() as s:
        home = await auth_service.home_tenant(s)
    if home is not None:
        r = await client.delete(
            f"/control/api/tenants/{home.slug}?confirm={home.slug}", headers=AUTH
        )
        assert r.status_code == 400


async def test_owner_can_change_plan_and_extend(client) -> None:
    await _signup(client)
    async with async_session_factory() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.owner_phone == PHONE))
        ).scalar_one()
        slug = t.slug
    r = await client.patch(
        f"/control/api/tenants/{slug}", headers=AUTH,
        json={"plan": "growth", "extend_days": 30, "onboarding_done": True},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["plan"] == "growth" and d["status"] == TENANT_ACTIVE
    assert d["onboarding_done"] is True and d["mrr_inr"] == 3999
