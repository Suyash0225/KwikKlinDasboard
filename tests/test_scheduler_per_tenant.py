"""Scheduler 60 dukaanon ke liye — har dukaan apne context mein.

Pehle jobs bina tenant ke chalti thin (RLS off, ORM filter off): daily
summary SAB dukaanon ke order gin kar .env wale malik ko jaata tha, payment
reminder doosri dukaan ke grahak ko is dukaan ke number se, aur sent_events
ki global key se dukaan A ka 'daysum:<date>' dukaan B ka summary rok deta.
Ye file wahi teen cheezein pakadti hai."""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models.tenant import Tenant
from app.services import scheduler, tenant_context, whatsapp

SLUG_B = "test-sched-b"
OWNER_B = "+919999900301"


@pytest.fixture
async def tenant_b():
    async with async_session_factory() as db:
        t = Tenant(
            slug=SLUG_B, shop_name="Sched B", owner_name="B", plan="growth",
            owner_phone=OWNER_B, status="active",
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
    tenant_context.invalidate_owner_cache()
    try:
        yield t
    finally:
        tenant_context.current_tenant_id.set(None)
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM sent_events WHERE event_key LIKE :a OR event_key LIKE :b"), {"a": "ttest%", "b": "%:ttest%"})
            await db.execute(sqltext("DELETE FROM tenants WHERE slug = :s"), {"s": SLUG_B})
            await db.commit()
        tenant_context.invalidate_owner_cache()


async def test_manager_phone_follows_the_tenant(tenant_b) -> None:
    """Context ke bahar .env ka malik; dukaan B ke andar B ka malik."""
    assert tenant_context.manager_phone() == settings.MANAGER_PHONE
    async with tenant_context.as_tenant(tenant_b.id):
        assert tenant_context.manager_phone() == OWNER_B
    assert tenant_context.manager_phone() == settings.MANAGER_PHONE


async def test_placeholder_owner_phone_falls_back_to_env(tenant_b) -> None:
    """Bootstrap ka +910000000000 jaisa kachra number normalize nahi hota —
    tab .env wala, taaki call-site par normalize_phone kabhi na phate."""
    async with async_session_factory() as db:
        t = await db.get(Tenant, tenant_b.id)
        t.owner_phone = "+910000000000"
        await db.commit()
    tenant_context.invalidate_owner_cache()
    async with tenant_context.as_tenant(tenant_b.id):
        assert tenant_context.manager_phone() == settings.MANAGER_PHONE


async def test_unconnected_shop_never_borrows_the_env_number(tenant_b) -> None:
    """Dukaan B ne WhatsApp nahi joda -> uske grahak ko home ke number se
    kuch NAHI jaata. Saaf mana, chupke se fallback nahi."""
    async with tenant_context.as_tenant(tenant_b.id):
        async with async_session_factory() as db:
            with pytest.raises(whatsapp.SendError) as exc:
                await whatsapp.resolve_creds(db)
    assert "not connected" in str(exc.value)
    assert exc.value.transient is False


async def test_idempotency_keys_are_per_shop(tenant_b) -> None:
    """Wahi event key do dukaanon mein alag-alag claim hoti hai."""
    home = await tenant_context.get_home_tenant_id()
    key = "ttest:daysum:2026-01-01"
    async with tenant_context.as_tenant(home):
        assert await scheduler._claim(key) is True
        assert await scheduler._claim(key) is False        # wahi dukaan, dobara nahi
    async with tenant_context.as_tenant(tenant_b.id):
        assert await scheduler._claim(key) is True         # doosri dukaan, apna
    async with async_session_factory() as db:
        n = (
            await db.execute(
                sqltext("SELECT count(*) FROM sent_events WHERE event_key LIKE :k"), {"k": "%ttest:daysum%"}
            )
        ).scalar_one()
    assert n == 2


async def test_active_tenants_skips_locked_shops(tenant_b) -> None:
    """Band dukaan ke grahak ko reminder nahi jaana chahiye."""
    ids = [t[0] for t in await tenant_context.active_tenants()]
    assert tenant_b.id in ids
    async with async_session_factory() as db:
        t = await db.get(Tenant, tenant_b.id)
        t.status = "locked"
        await db.commit()
    ids = [t[0] for t in await tenant_context.active_tenants()]
    assert tenant_b.id not in ids


async def test_for_each_tenant_isolates_one_shops_crash(tenant_b, monkeypatch) -> None:
    """Ek dukaan ka job phata to baaki chalte hain, aur har run apne
    tenant context mein hota hai."""
    seen: list = []

    async def job(now):
        tid = tenant_context.current_tenant_id.get()
        seen.append(tid)
        if tid == tenant_b.id:
            raise RuntimeError("boom")

    await scheduler._for_each_tenant("t", job)
    home = await tenant_context.get_home_tenant_id()
    assert tenant_b.id in seen and home in seen
    assert tenant_context.current_tenant_id.get() is None   # context saaf
