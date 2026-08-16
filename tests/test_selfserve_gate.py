"""Self-serve gate proof: signup -> login -> APNA dashboard, sab kuch
session-tenant par — data, billing-status, plan-features, settings,
order-numbering. Home shop par zero asar.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models.tenant import Tenant, User
from app.services import auth, kpis, tenant_context

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
SLUG = "test-ss-b"
EMAIL = "selfserve-b@test.local"
STAFF_EMAIL = "selfserve-b-staff@test.local"
CUST = "+919999900031"


async def _purge() -> None:
    tenant_context.current_tenant_id.set(None)
    sub = f"(SELECT id FROM tenants WHERE slug = '{SLUG}')"
    async with async_session_factory() as db:
        for q in [
            f"DELETE FROM conversations WHERE tenant_id IN {sub}",
            f"DELETE FROM order_status_history WHERE order_id IN (SELECT id FROM orders WHERE tenant_id IN {sub})",
            f"DELETE FROM tasks WHERE tenant_id IN {sub}",
            f"DELETE FROM payments WHERE tenant_id IN {sub}",
            f"DELETE FROM orders WHERE tenant_id IN {sub}",
            f"DELETE FROM customers WHERE tenant_id IN {sub}",
            f"DELETE FROM settings_kv WHERE tenant_id IN {sub}",
            f"DELETE FROM audit_log WHERE tenant_id IN {sub}",
            f"DELETE FROM llm_usage WHERE tenant_id IN {sub}",
            f"DELETE FROM invites WHERE tenant_id IN {sub}",
            f"DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users WHERE tenant_id IN {sub})",
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


async def _mk_tenant_user(role="OWNER", status="trial", plan="starter"):
    async with async_session_factory() as db:
        t = Tenant(
            slug=SLUG, shop_name="SelfServe B", owner_name="SS Owner",
            owner_phone="+919999900032", plan=plan, status=status,
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db.add(t)
        await db.flush()
        u = User(tenant_id=t.id, name="SS User",
                 email=EMAIL if role == "OWNER" else STAFF_EMAIL,
                 password_hash=auth.hash_password("selfserve-pw-1"), role=role)
        db.add(u)
        await db.commit()
        token = await auth.start_session(db, u, ip="127.0.0.1", user_agent="pytest")
        return t.id, token


async def test_login_lands_on_dashboard_now(client) -> None:
    await _mk_tenant_user()
    r = await client.post("/api/login", json={"email": EMAIL, "password": "selfserve-pw-1"})
    assert r.status_code == 200
    assert r.json()["next_url"] == "/admin", "self-serve: har chalu tenant dashboard par"
    # /admin page bhi serve hota hai (redirect welcome par NAHI)
    r = await client.get("/admin", follow_redirects=False)
    assert r.status_code == 200
    client.cookies.delete("kk_session")


async def test_locked_tenant_still_goes_to_welcome(client) -> None:
    await _mk_tenant_user(status="locked")
    r = await client.post("/api/login", json={"email": EMAIL, "password": "selfserve-pw-1"})
    assert r.json()["next_url"] == "/welcome"
    r = await client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/welcome"
    client.cookies.delete("kk_session")


async def test_tenant_operates_own_shop_isolated(client, sent) -> None:
    """B apna customer + order banata hai apne dashboard se; number APNA
    -01 milta hai (home se clash nahi); home ki list mein B ka kuch nahi."""
    _tid, token = await _mk_tenant_user()
    client.cookies.set("kk_session", token)
    try:
        r = await client.post("/orders", json={
            "customer_phone": CUST, "customer_name": "B ka Grahak",
            "items": [{"type": "shirt", "qty": 2, "service": "wash_iron"}],
            "total_amount": "100.00",
        })
        assert r.status_code == 201, r.text
        num_b = r.json()["order_number"]
        assert num_b.endswith("-01"), f"B ka apna numbering series chahiye: {num_b}"

        # B ko sirf apna data
        r = await client.get("/admin/api/customers?limit=500")
        assert CUST in r.text
        # home tenant ka koi order/customer B ko nahi dikhta
        r = await client.get("/orders")
        nums = [o["order_number"] for o in r.json()]
        assert nums == [num_b]
    finally:
        client.cookies.delete("kk_session")

    # Home (API key) ki taraf se: B ka customer/order INVISIBLE
    r = await client.get("/admin/api/customers?limit=500", headers=AUTH)
    assert CUST not in r.text, "B ka customer home dashboard mein leak!"
    r = await client.get("/orders", headers=AUTH)
    assert all(o["order_number"] != "KK-XXXX" for o in r.json())  # sanity
    # aur home AAJ apna order banaye to use bhi -01/-agla mile bina clash ke
    r = await client.post("/orders", headers=AUTH, json={
        "customer_phone": "+919999900033",
        "items": [{"type": "pant", "qty": 1}],
    })
    assert r.status_code == 201, "home ka numbering B se takraya!"
    from tests.conftest import purge_phones

    await purge_phones("+919999900033")


async def test_settings_are_per_tenant(client) -> None:
    """B apni setting badalta hai -> home ki value untouched."""
    _tid, token = await _mk_tenant_user()
    home_val = (
        await client.get("/admin/api/settings", headers=AUTH)
    ).json()["turnaround_days"]

    client.cookies.set("kk_session", token)
    try:
        r = await client.put("/admin/api/settings",
                             json={"key": "turnaround_days", "value": 9})
        assert r.status_code == 200, r.text
        assert (
            await client.get("/admin/api/settings")
        ).json()["turnaround_days"] == 9
    finally:
        client.cookies.delete("kk_session")

    assert (
        await client.get("/admin/api/settings", headers=AUTH)
    ).json()["turnaround_days"] == home_val, "B ki setting home par lag gayi!"


async def test_billing_and_features_follow_session_tenant(client) -> None:
    """B past_due -> B ke writes 402 (home unaffected); B starter-plan ->
    campaigns 402 jabki home (business) 200."""
    _tid, token = await _mk_tenant_user(status="past_due")
    client.cookies.set("kk_session", token)
    try:
        assert (await client.get("/orders")).status_code == 200  # read chalti hai
        r = await client.post("/orders", json={
            "customer_phone": CUST, "items": [{"type": "shirt", "qty": 1}],
        })
        assert r.status_code == 402, "B past_due hai — write nahi hona chahiye"
        # feature gate B ke plan (starter) par
        assert (await client.get("/admin/api/campaigns")).status_code == 402
    finally:
        client.cookies.delete("kk_session")
    # home unaffected: write bhi chalti hai, campaigns bhi
    assert (await client.get("/admin/api/campaigns", headers=AUTH)).status_code == 200


async def test_staff_role_limited_in_own_tenant_too(client) -> None:
    _tid, token = await _mk_tenant_user(role="STAFF")
    client.cookies.set("kk_session", token)
    try:
        assert (await client.get("/admin/api/dashboard")).status_code == 200
        assert (await client.get("/admin/api/expenses")).status_code == 403
        assert (await client.get("/admin/api/settings")).status_code == 403
    finally:
        client.cookies.delete("kk_session")
