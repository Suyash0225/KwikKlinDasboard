"""Har plan apne hi darwaze kholta hai — poora matrix, guess nahi.

Sawaal jo ye file poochti hai: ₹999 wala ₹4999 wale ke features to nahi
chala raha? Isliye har gated route ko TEENO plans par maara jaata hai aur
jawab plans.py se hi nikala jaata hai — koi hardcoded list nahi, to naya
feature/plan add karne par test apne aap us par bhi chalega.

Do aur cheezein yahan hain kyunki asli paisa inhi par hai:
- limits (orders/month, staff, outlets, campaign msgs) enforce hote hain
- /api/me ka `features` array plan se milta hai (frontend isi se tab lock
  karta hai — backend aur UI kabhi alag na bolein)
"""

import dataclasses

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.services import plans, tenant_context

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}

# (method, path, feature) — jo bhi require_feature() se gated hai.
# GET hi rakhe hain jahan ho sake: mutation ka 200/422 shor daalta hai,
# hum sirf "402 aaya ya nahi" dekh rahe hain.
GATED_ROUTES = [
    ("GET", "/admin/api/inbox/threads", "inbox"),
    ("GET", "/admin/api/reports/summary", "reports"),
    ("GET", "/admin/api/export/orders.csv", "reports"),
    ("GET", "/admin/api/export/customers.csv", "reports"),
    ("GET", "/admin/api/usage", "reports"),
    ("GET", "/admin/api/whatsapp/stats", "reports"),
    ("GET", "/admin/api/campaigns", "campaigns"),
    ("GET", "/admin/api/segments", "campaigns"),
    ("GET", "/admin/api/coupons", "campaigns"),
    ("GET", "/admin/api/leads", "marketing_agent"),
    ("GET", "/admin/api/training/faq", "service_agent"),
    ("GET", "/admin/api/training/corrections", "service_agent"),
    ("GET", "/admin/api/training/docs", "service_agent"),
    ("GET", "/admin/api/training/teachme", "service_agent"),
    ("GET", "/admin/api/agents/overview", "service_agent"),
    ("GET", "/orders", "billing"),
]


async def _set_home_plan(code: str) -> None:
    async with async_session_factory() as db:
        await db.execute(
            sqltext("UPDATE tenants SET plan = :p, limit_overrides = '{}' WHERE slug = 'kwik-klin'"),
            {"p": code},
        )
        await db.commit()


@pytest.fixture
async def restore_plan():
    yield
    tenant_context.current_tenant_id.set(None)
    await _set_home_plan("growth")


# ------------------------------------------------------------- the matrix --

@pytest.mark.parametrize("plan_code", ["starter", "pro", "growth"])
async def test_every_gated_route_matches_the_plan(client, restore_plan, plan_code) -> None:
    """Plan mein feature hai -> khulta hai; nahi hai -> 402 Upgrade.

    Ye assertion plans.py se derive hoti hai, isliye plan badla to test
    apne aap naye sach par chalta hai — aur galti se feature khol dene par
    laal ho jaata hai."""
    await _set_home_plan(plan_code)
    for method, path, feature in GATED_ROUTES:
        allowed = plans.feature_on(plan_code, feature)
        r = await client.request(method, path, headers=AUTH)
        if allowed:
            assert r.status_code != 402, (
                f"{plan_code}: {path} ({feature}) plan mein hai par 402 mila"
            )
        else:
            assert r.status_code == 402, (
                f"{plan_code}: {path} ({feature}) plan mein NAHI hai par "
                f"{r.status_code} mila — paid feature muft mil raha hai"
            )
            assert "Upgrade" in r.json()["detail"]


async def test_basic_pays_less_and_gets_less(client, restore_plan) -> None:
    """₹999 vs ₹1999 vs ₹4999 ka farak sach mein code mein hai."""
    basic, premium, business = (plans.get(c) for c in ("starter", "pro", "growth"))
    assert (basic.price_inr, premium.price_inr, business.price_inr) == (999, 1999, 4999)
    # har upar wala plan neeche wale ka superset ho — warna upgrade karne par
    # kuch chhin jaayega, jo bikri mein sabse bura anubhav hai
    assert basic.features < premium.features < business.features
    # aur limits kabhi ghatti nahi (None = unlimited = sabse upar; 0 asli
    # cap hai — Basic mein campaigns hai hi nahi, isliye 0)
    for lim in ("ai_usage_limit", "whatsapp_message_limit", "max_campaign_msgs_month"):
        vals = [(10**9 if getattr(p, lim) is None else getattr(p, lim))
                for p in (basic, premium, business)]
        assert vals == sorted(vals), f"{lim} upgrade par ghat raha hai: {vals}"


# ------------------------------------------------------------------ /api/me --

async def test_api_me_features_match_the_plan(client, restore_plan) -> None:
    """Frontend tabs isi array se lock hote hain — backend se alag hua to
    user ko tab dikhega aur click par 402 milega."""
    from app.models.tenant import Tenant
    from app.services import auth

    async with async_session_factory() as db:
        t = (await db.execute(sqltext("SELECT id FROM tenants WHERE slug='kwik-klin'"))).scalar_one()
        u = (
            await db.execute(
                sqltext("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": t}
            )
        ).scalar_one_or_none()
        if u is None:
            pytest.skip("home tenant ka koi user nahi")
        user = await db.get(auth.User if hasattr(auth, "User") else Tenant, u) if False else None

    for code in ("starter", "pro", "growth"):
        await _set_home_plan(code)
        async with async_session_factory() as db:
            from app.models import User as _U

            user = (await db.execute(sqltext("SELECT id FROM users WHERE tenant_id = :t LIMIT 1"), {"t": t})).scalar_one()
            u_obj = await db.get(_U, user)
            token = await auth.start_session(db, u_obj, ip="127.0.0.1", user_agent="pytest")
        client.cookies.set("kk_session", token)
        try:
            r = await client.get("/api/me")
            assert r.status_code == 200, r.text
            got = set(r.json().get("features") or [])
            assert got == set(plans.get(code).features), (
                f"{code}: /api/me {got} vs plan {set(plans.get(code).features)}"
            )
        finally:
            client.cookies.delete("kk_session")


# ------------------------------------------------------------------ limits --

async def test_order_limit_is_enforced_per_plan(client, restore_plan) -> None:
    """Basic ki 400 orders/month asli hai — 401st par 402, na ki chup-chaap
    pass. Limit 0 karke ek hi order se sabit hota hai."""
    orig = plans.PLANS["starter"]
    plans.PLANS["starter"] = dataclasses.replace(orig, max_orders_month=0)
    await _set_home_plan("starter")
    try:
        r = await client.post(
            "/orders", headers=AUTH,
            json={"customer_phone": "9999900771",
                  "items": [{"type": "Shirt", "service": "Wash", "qty": 1}]},
        )
        assert r.status_code == 402, (
            f"limit paar karke bhi order ban gaya / galat code: {r.status_code} {r.text[:120]}"
        )
        assert "Upgrade" in r.json()["detail"]
    finally:
        plans.PLANS["starter"] = orig


async def test_unlimited_plans_are_actually_unlimited(restore_plan) -> None:
    """Premium/Business par orders unlimited — None matlab None, 0 nahi."""
    assert plans.get("pro").max_orders_month is None
    assert plans.get("growth").max_orders_month is None
    assert plans.get("starter").max_orders_month == 400


async def test_staff_and_outlet_limits_differ_by_plan() -> None:
    assert plans.get("starter").max_staff == 3
    assert plans.get("pro").max_staff == 8
    assert plans.get("growth").max_staff is None
    assert plans.get("starter").max_outlets == 1
    assert plans.get("growth").max_outlets == 3


async def test_vendor_override_beats_the_plan(restore_plan) -> None:
    """Control panel se di gayi chhoot plan se upar hai (-1 = unlimited) —
    warna vendor ek client ko extra dene ke liye plan hi badalta."""
    class _T:
        plan = "starter"
        limit_overrides = {"max_orders_month": -1, "max_staff": 25}

    lim = plans.effective_limits(_T())
    assert lim["max_orders_month"] is None
    assert lim["max_staff"] == 25
    # jo override nahi hua wo plan se hi aaye
    assert lim["whatsapp_message_limit"] == plans.get("starter").whatsapp_message_limit


# staff panel ka do-dukaan setup wahi hai jo test_staff_panel.py mein hai —
# dobara likhne se do jagah maintain karna padta.
from tests.test_staff_panel import two_shops  # noqa: E402  (fixture re-export)


# ------------------------------------------------------------ staff panel --
# Staff panel ke apne gates hain (require_staff_feature). ₹999 wale ka
# delivery boy COD collect na kar paaye, cancel na maang paaye, manager
# reports na dekh paaye — yahi Premium/Business ka farak hai.

STAFF_GATED = [
    ("GET", "/staff/api/bills", "billing"),
    ("GET", "/staff/api/customers/search?q=ab", "billing"),
    ("GET", "/staff/api/team", "staff_reports"),
]


@pytest.mark.parametrize("plan_code", ["starter", "pro", "growth"])
async def test_staff_panel_routes_match_the_plan(
    client, two_shops, sent, restore_plan, plan_code
) -> None:
    from tests.test_staff_panel import A_MGR_PHONE, _login

    async with async_session_factory() as db:
        await db.execute(
            sqltext("UPDATE tenants SET plan = :p WHERE id = :t"),
            {"p": plan_code, "t": two_shops["a"]},
        )
        await db.commit()
    await _login(client, A_MGR_PHONE)
    try:
        for method, path, feature in STAFF_GATED:
            allowed = plans.feature_on(plan_code, feature)
            r = await client.request(method, path)
            if allowed:
                assert r.status_code != 402, f"{plan_code}: {path} band hai par plan mein hai"
            else:
                assert r.status_code == 402, (
                    f"{plan_code}: {path} ({feature}) plan mein nahi par "
                    f"{r.status_code} — muft mil raha hai"
                )
    finally:
        client.cookies.clear()


async def test_cod_and_cancel_are_premium_only() -> None:
    """₹999 ka delivery boy paisa collect nahi kar sakta, ₹1999 ka kar sakta."""
    assert not plans.feature_on("starter", "cod_collection")
    assert not plans.feature_on("starter", "cancel_approval")
    assert not plans.feature_on("starter", "staff_roles")
    for f in ("cod_collection", "cancel_approval", "staff_roles", "staff_reports"):
        assert plans.feature_on("pro", f), f
    # barcode aur multi-branch sirf Business
    for f in ("barcode_tracking", "multi_branch"):
        assert not plans.feature_on("pro", f), f
        assert plans.feature_on("growth", f), f


async def test_basic_staff_all_look_the_same(two_shops, sent) -> None:
    """staff_roles off -> DB mein MANAGER likha ho tab bhi manager powers
    nahi. Downgrade apne aap sahi behave kare."""
    from app.models import Staff
    from app.services.staff_auth import StaffPrincipal
    from app.models.tenant import Tenant

    async def _principal(plan: str):
        """Har baar naya session — warna SQLAlchemy purana cached Tenant deta hai."""
        async with async_session_factory() as db:
            await db.execute(
                sqltext("UPDATE tenants SET plan = :p WHERE id = :t"),
                {"p": plan, "t": two_shops["a"]},
            )
            await db.commit()
        async with async_session_factory() as db:
            t = await db.get(Tenant, two_shops["a"])
            mgr = await db.get(Staff, two_shops["a_mgr"])
            return StaffPrincipal(mgr, t).is_manager

    assert await _principal("starter") is False
    assert await _principal("pro") is True
