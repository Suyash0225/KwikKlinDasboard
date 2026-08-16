"""Plans, feature flags aur quota enforcement ka proof.

1. Config sanity: har plan ke flags registered FEATURE_KEYS mein se hain;
   Basic/Premium/Business naam + naye-naam aliases kaam karte hain.
2. Gates: Basic plan par campaigns/reports/leads endpoints 402 "Upgrade"
   dete hain; Business par sab khulta hai. AI reply bhi feature se bandh.
3. Quota: AI calls aur WhatsApp messages per-tenant metered hain aur limit
   par enforce hote hain (LLM -> degrade path, WA -> loud SendError).
"""

import dataclasses

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.services import plans, quota, tenant_context
from app.services.quota import QuotaExceeded

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


async def _set_home_plan(code: str) -> None:
    async with async_session_factory() as db:
        await db.execute(
            sqltext("UPDATE tenants SET plan = :p WHERE slug = 'kwik-klin'"),
            {"p": code},
        )
        await db.commit()


@pytest.fixture
async def _restore_plan():
    yield
    tenant_context.current_tenant_id.set(None)
    await _set_home_plan("growth")


# ---------------------------------------------------------------- config --

def test_plan_config_sanity() -> None:
    assert set(plans.PLANS) == {"starter", "pro", "growth"}
    for p in plans.PLANS.values():
        assert p.features <= set(plans.FEATURE_KEYS), f"{p.code}: unknown flag"
    # naming: bikne wale naam
    assert plans.get("starter").name == "Basic"
    assert plans.get("pro").name == "Premium"
    assert plans.get("growth").name == "Business"
    # naye naam alias ki tarah chalte hain
    assert plans.get("premium").code == "pro"
    assert plans.get("business").code == "growth"
    # Basic mein marketing band, AI on (PRD: AI har plan mein)
    assert plans.feature_on("starter", "service_agent")
    assert not plans.feature_on("starter", "campaigns")
    assert not plans.feature_on("starter", "marketing_agent")
    # Business = sab kuch
    assert plans.get("growth").features == set(plans.FEATURE_KEYS)
    # upgrade hint
    assert plans.plan_with_feature("campaigns") == "pro"
    assert plans.plan_with_feature("marketing_agent") == "growth"


# ----------------------------------------------------------------- gates --

async def test_basic_plan_locks_premium_features(client, _restore_plan) -> None:
    await _set_home_plan("starter")
    for path in ("/admin/api/campaigns", "/admin/api/reports/summary",
                 "/admin/api/leads", "/admin/api/usage", "/admin/api/segments"):
        r = await client.get(path, headers=AUTH)
        assert r.status_code == 402, f"{path}: {r.status_code}"
        assert "Upgrade" in r.json()["detail"], path
    # jo Basic mein hai wo chalta rahe
    assert (await client.get("/orders", headers=AUTH)).status_code == 200
    assert (await client.get("/admin/api/inbox/threads", headers=AUTH)).status_code == 200


async def test_business_plan_opens_everything(client, _restore_plan) -> None:
    await _set_home_plan("growth")
    for path in ("/admin/api/campaigns", "/admin/api/reports/summary",
                 "/admin/api/leads", "/admin/api/usage"):
        r = await client.get(path, headers=AUTH)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:100]}"


async def test_service_agent_gate_falls_back_to_rules(_restore_plan) -> None:
    """Feature off -> build_ai_reply None deta hai (deterministic replies
    chalti rehti hain, AI nahi chalta)."""
    from app.models import Customer
    from app.services.ai_agent import build_ai_reply

    await _set_home_plan("starter")
    # starter mein service_agent ON hai — gate pass, LLM blocked in tests
    # isliye sirf OFF wala case yahan test hota hai: custom plan bana ke.
    orig = plans.PLANS["starter"]
    plans.PLANS["starter"] = dataclasses.replace(
        orig, features=frozenset({"billing", "inbox"})
    )
    try:
        async with async_session_factory() as db:
            c = Customer(phone="+919999900091", name="GateTest")
            db.add(c)
            await db.flush()
            reply = await build_ai_reply(db, c, "kitne ka dry clean hai?")
            await db.rollback()
        assert reply is None, "feature off par AI reply nahi aana chahiye"
    finally:
        plans.PLANS["starter"] = orig


# ---------------------------------------------------------------- quotas --

async def test_ai_quota_enforced(_restore_plan) -> None:
    orig = plans.PLANS["growth"]
    plans.PLANS["growth"] = dataclasses.replace(orig, ai_usage_limit=0)
    try:
        with pytest.raises(QuotaExceeded) as exc_info:
            await quota.check_ai_quota()
        assert "AI usage" in str(exc_info.value)
        # LLM entry isi ko LLMUnavailable mein badalta hai (degrade path)
        from app.services import llm_client

        with pytest.raises(llm_client.LLMUnavailable):
            await llm_client.ask(system="s", user_text="hi")
    finally:
        plans.PLANS["growth"] = orig
    # limit hata do to pass
    await quota.check_ai_quota()


async def test_wa_quota_enforced(monkeypatch, _restore_plan) -> None:
    from app.services import whatsapp

    orig = plans.PLANS["growth"]
    plans.PLANS["growth"] = dataclasses.replace(orig, whatsapp_message_limit=0)

    async def _never_called(payload, to_phone):  # network tak pahunchna hi nahi chahiye
        raise AssertionError("quota se pehle hi rukna chahiye tha")

    monkeypatch.setattr(whatsapp, "_post_with_retry", _never_called)
    try:
        async with async_session_factory() as db:
            with pytest.raises(whatsapp.SendError) as exc_info:
                await whatsapp.send_message(
                    db, to_phone="+919999900092", text="test", enqueue_on_fail=False
                )
        assert "limit" in str(exc_info.value)
    finally:
        plans.PLANS["growth"] = orig


async def test_meters_count_per_tenant() -> None:
    """Meters tenant-scoped hain — doosre tenant ke counts mein nahi girte."""
    home = await tenant_context.get_home_tenant_id()
    async with async_session_factory() as db:
        home_ai = await quota.ai_calls_this_month(db, home)
        home_wa = await quota.wa_messages_this_month(db, home)
        import uuid as _uuid

        ghost = _uuid.uuid4()  # aisa tenant jo hai hi nahi
        assert await quota.ai_calls_this_month(db, ghost) == 0
        assert await quota.wa_messages_this_month(db, ghost) == 0
    assert home_ai >= 0 and home_wa >= 0
