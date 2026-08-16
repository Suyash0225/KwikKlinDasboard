"""Client ka apna billing page + recharge request -> vendor approve.

Aur sabse zaroori: BILLABLE meter — customer ke message ka jawab (service,
Meta par free) kabhi limit nahi khaata; sirf template/marketing khaate hain.
"""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models.tenant import Tenant, User
from app.services import auth, plans, quota, tenant_context

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
SLUG = "test-bill-a"
EMAIL = "bill-a@test.local"


async def _purge() -> None:
    tenant_context.current_tenant_id.set(None)
    sub = f"(SELECT id FROM tenants WHERE slug = '{SLUG}')"
    async with async_session_factory() as db:
        for q in [
            f"DELETE FROM recharge_requests WHERE tenant_id IN {sub}",
            f"DELETE FROM credit_ledger WHERE tenant_id IN {sub}",
            f"DELETE FROM conversations WHERE tenant_id IN {sub}",
            f"DELETE FROM customers WHERE tenant_id IN {sub}",
            f"DELETE FROM audit_log WHERE tenant_id IN {sub}",
            f"DELETE FROM settings_kv WHERE tenant_id IN {sub}",
            f"DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users WHERE tenant_id IN {sub})",
            f"DELETE FROM users WHERE tenant_id IN {sub}",
            f"DELETE FROM tenants WHERE slug = '{SLUG}'",
        ]:
            await db.execute(sqltext(q))
        await db.commit()


@pytest.fixture(autouse=True)
async def _cleanup():
    await _purge()
    yield
    await _purge()


@pytest.fixture
async def shop():
    async with async_session_factory() as db:
        t = Tenant(slug=SLUG, shop_name="Billing Test Shop", owner_name="BT",
                   owner_phone="+919999900011", plan="starter", status="active")
        db.add(t)
        await db.flush()
        u = User(tenant_id=t.id, name="BT Owner", email=EMAIL,
                 password_hash=auth.hash_password("billing-pw-1"), role="OWNER")
        db.add(u)
        await db.commit()
        await db.refresh(t)
        token = await auth.start_session(db, u, ip="127.0.0.1", user_agent="pytest")
        return t, token


# ---------------------------------------------------- billable meter -----

async def test_free_service_replies_never_eat_the_quota(shop) -> None:
    """Ye poore pricing model ki jaan hai: customer ke window mein diya
    gaya jawab Meta par FREE hai — wo limit mein ginna hi nahi chahiye."""
    from app.models import Conversation, Customer
    from app.models.enums import Direction

    t, _ = shop
    tok = tenant_context.current_tenant_id.set(t.id)
    try:
        async with async_session_factory() as db:
            c = Customer(phone="+919999900012", name="Grahak", tenant_id=t.id)
            db.add(c)
            await db.flush()
            for cat, n in (("service", 40), ("utility", 3), ("marketing", 2)):
                for i in range(n):
                    db.add(Conversation(
                        customer_id=c.id, direction=Direction.OUTBOUND,
                        message_text=f"{cat} {i}", billing_category=cat, tenant_id=t.id,
                    ))
            await db.commit()
            billable = await quota.wa_messages_this_month(db, t.id)
    finally:
        tenant_context.current_tenant_id.reset(tok)
    assert billable == 5, f"sirf utility+marketing ginne chahiye, gine {billable}"


def test_plan_economics_have_margin() -> None:
    """Har plan ki lagat plan ke daam se kaafi kam ho — warna loss."""
    # Worst case = marketing cap poora (₹0.80/msg, sabse mehnga), baaki
    # billable utility, AI limit poori, + server share. Dono cap enforce
    # hote hain (quota + campaign budget), isliye isse upar ja hi nahi sakta.
    for code in ("starter", "pro", "growth"):
        p = plans.get(code)
        m = plans.margin(code)
        assert m >= 0.35, (
            f"{p.name}: worst-case margin sirf {m:.0%} "
            f"(cost ₹{plans.worst_case_cost(code):.0f} vs price ₹{p.price_inr})"
        )
        assert p.max_campaign_msgs_month <= (p.whatsapp_message_limit or 0), \
            f"{p.name}: marketing cap billable limit se zyada hai — hisaab jhootha"

    for pack in plans.RECHARGE_PACKS:
        assert pack["price_inr"] > pack["cost_inr"], f"{pack['id']} par loss"
    assert all("cost_inr" not in p for p in plans.public_packs()), \
        "client ko lagat kabhi nahi dikhni chahiye"


# ------------------------------------------------ client billing page ----

async def test_summary_and_recharge_request_flow(client, shop) -> None:
    t, token = shop
    client.cookies.set("kk_session", token)
    try:
        d = (await client.get("/api/billing/summary")).json()
        assert d["shop"] == "Billing Test Shop"
        assert d["plan"]["name"] == "Basic"
        assert d["usage"]["messages"]["limit"] == plans.get("starter").whatsapp_message_limit
        assert d["packs"] and "cost_inr" not in d["packs"][0]

        r = await client.post("/api/billing/recharge-request",
                              json={"pack": "wa-1000", "note": "UPI 4477"})
        assert r.status_code == 201 and r.json()["amount_inr"] == 1500

        rows = (await client.get("/api/billing/requests")).json()
        assert rows[0]["pack"] == "wa-1000" and rows[0]["status"] == "pending"

        assert (await client.post("/api/billing/recharge-request",
                                  json={"pack": "nahi-hai"})).status_code == 400
    finally:
        client.cookies.delete("kk_session")

    # vendor: dikhta hai, approve par credits chadh jaate hain
    pend = (await client.get("/control/api/recharge-requests", headers=AUTH)).json()
    mine = next(x for x in pend if x["tenant"] == SLUG)
    assert mine["amount_inr"] == 1500 and mine["note"] == "UPI 4477"

    r = await client.post(f"/control/api/recharge-requests/{mine['id']}/decide",
                          headers=AUTH, json={"approve": True, "note": "GPay ok"})
    assert r.status_code == 200 and r.json()["balance"] == 1000

    async with async_session_factory() as db:
        t2 = await db.get(Tenant, t.id)
        assert t2.wa_credits == 1000

    # dobara decide -> 409 (ek hi baar credits)
    r = await client.post(f"/control/api/recharge-requests/{mine['id']}/decide",
                          headers=AUTH, json={"approve": True})
    assert r.status_code == 409

    hist = (await client.get(f"/control/api/tenants/{SLUG}/credits", headers=AUTH)).json()
    assert hist["history"][0]["amount"] == 1000 and "wa-1000" in hist["history"][0]["reason"]


async def test_billing_page_and_renew_card_exist(client) -> None:
    html = (await client.get("/billing")).text
    assert "Plan &amp; Recharge" in html or "Plan & Recharge" in html
    assert "Request this pack" in html and "free" in html
    js = (await client.get("/admin/static/app.js")).text
    assert "renewCard" in js and "Remind me later" in js and "/billing" in js


async def test_marketing_cap_comes_from_the_plan(shop, monkeypatch) -> None:
    """Campaign budget plan ke marketing cap se zyada kabhi nahi ho sakta —
    yahi wo cheez hai jo plan ko loss se bachati hai."""
    from app.services import app_settings, marketing

    t, _ = shop  # Basic plan: marketing cap 0
    seen = {}

    async def _fake_get(db, key):
        if key == "marketing_monthly_msg_budget":
            return 99999          # owner ne bada budget rakha ho tab bhi
        return await app_settings.DEFAULTS.get(key)

    tok = tenant_context.current_tenant_id.set(t.id)
    try:
        async with async_session_factory() as db:
            cap = plans.effective_limits(t)["max_campaign_msgs_month"]
            assert cap == 0, "Basic mein marketing bilkul band honi chahiye"
        # Premium par cap 500
        async with async_session_factory() as db:
            t2 = await db.get(type(t), t.id)
            t2.plan = "pro"
            await db.commit()
            t2 = await db.get(type(t), t.id)
            assert plans.effective_limits(t2)["max_campaign_msgs_month"] == 500
    finally:
        tenant_context.current_tenant_id.reset(tok)
