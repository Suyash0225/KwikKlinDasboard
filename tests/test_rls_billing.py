"""Billing tables par RLS — invoices/billing_events/credit_ledger/
recharge_requests (migration r2a8c5d3f9e7).

Pehle ye sirf app-level WHERE par tike the. Ab Postgres khud: dukaan ke
context mein doosri dukaan ka invoice na dikhta hai, na likha ja sakta
hai; system context (control panel, Razorpay webhook) ko sab."""

import pytest
from sqlalchemy import select, text as sqltext
from sqlalchemy.exc import DBAPIError

from app.database import async_session_factory
from app.models.tenant import Invoice, Tenant
from app.services import tenant_context

SLUG_B = "test-rls-b"
PAY_B = "pay_TESTRLSB0001"
PAY_X = "pay_TESTRLSX0001"


@pytest.fixture
async def tenant_b():
    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM invoices WHERE rzp_payment_id IN (:a, :b)"), {"a": PAY_B, "b": PAY_X})
        await db.execute(sqltext("DELETE FROM tenants WHERE slug = :s"), {"s": SLUG_B})
        await db.commit()
        t = Tenant(slug=SLUG_B, shop_name="RLS B", owner_name="B", plan="starter",
                   owner_phone="+919999900401", status="active")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        # system context se B ka invoice — jaise Razorpay webhook likhta hai
        db.add(Invoice(tenant_id=t.id, rzp_payment_id=PAY_B, plan="starter", amount_paise=99900))
        await db.commit()
    try:
        yield t
    finally:
        tenant_context.current_tenant_id.set(None)
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM invoices WHERE rzp_payment_id IN (:a, :b)"), {"a": PAY_B, "b": PAY_X})
            await db.execute(sqltext("DELETE FROM tenants WHERE slug = :s"), {"s": SLUG_B})
            await db.commit()


async def test_a_shop_sees_only_its_own_invoices(tenant_b) -> None:
    home = await tenant_context.get_home_tenant_id()
    # home ke context mein B ka invoice gayab — chahe WHERE na lagao
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            rows = (await db.execute(select(Invoice.rzp_payment_id))).scalars().all()
    assert PAY_B not in rows
    # B ke context mein apna dikhta hai
    async with tenant_context.as_tenant(tenant_b.id):
        async with async_session_factory() as db:
            rows = (await db.execute(select(Invoice.rzp_payment_id))).scalars().all()
    assert PAY_B in rows
    # system context (control panel) ko sab
    async with async_session_factory() as db:
        rows = (await db.execute(select(Invoice.rzp_payment_id))).scalars().all()
    assert PAY_B in rows


async def test_a_shop_cannot_write_another_shops_invoice(tenant_b) -> None:
    """WITH CHECK: B ke context se home ke naam ka invoice — Postgres mana."""
    home = await tenant_context.get_home_tenant_id()
    async with tenant_context.as_tenant(tenant_b.id):
        async with async_session_factory() as db:
            db.add(Invoice(tenant_id=home, rzp_payment_id=PAY_X, plan="starter", amount_paise=1))
            with pytest.raises(DBAPIError):
                await db.commit()
            await db.rollback()
    async with async_session_factory() as db:
        n = (await db.execute(sqltext("SELECT count(*) FROM invoices WHERE rzp_payment_id = :p"), {"p": PAY_X})).scalar_one()
    assert n == 0
