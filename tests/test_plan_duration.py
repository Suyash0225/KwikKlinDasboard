"""IMP_008: vendor panel se plan + muddat (1/3/6/12 mahine), subscription se juda."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text as sqltext

from app.database import async_session_factory
from app.models.tenant import Invoice, Tenant
from app.routers.orders import vendor_master_key
from app.services import plans

CTL = {"X-API-Key": vendor_master_key()}
SLUG = "test-plan-dur"


@pytest.fixture
async def shop():
    async def wipe():
        async with async_session_factory() as db:
            for q in ("DELETE FROM invoices WHERE tenant_id IN (SELECT id FROM tenants WHERE slug=:s)",
                      "DELETE FROM audit_log WHERE tenant_id IN (SELECT id FROM tenants WHERE slug=:s)",
                      "DELETE FROM tenants WHERE slug=:s"):
                await db.execute(sqltext(q), {"s": SLUG})
            await db.commit()
    await wipe()
    async with async_session_factory() as db:
        t = Tenant(slug=SLUG, shop_name="Plan Dur", owner_name="O", plan="starter",
                   owner_phone="+919999900281", status="trial",
                   trial_ends_at=datetime.now(timezone.utc) + timedelta(days=2))
        db.add(t)
        await db.commit()
    yield
    await wipe()


async def _tenant():
    async with async_session_factory() as db:
        return (await db.execute(select(Tenant).where(Tenant.slug == SLUG))).scalar_one()


def test_add_months_respects_month_ends() -> None:
    d = datetime(2026, 1, 31, tzinfo=timezone.utc)
    assert plans.add_months(d, 1).date().isoformat() == "2026-02-28"
    assert plans.add_months(d, 12).date().isoformat() == "2027-01-31"
    assert plans.price_for("pro", 3) == 1999 * 3 and plans.price_for("pro", 12) == 19990
    assert plans.monthly_value("pro", "annual") == round(19990 / 12)


async def test_duration_sets_period_status_cycle_and_invoice(client, shop) -> None:
    r = await client.patch(f"/control/api/tenants/{SLUG}", headers=CTL,
                           json={"plan": "pro", "duration_months": 6, "payment_received": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active" and body["billing_cycle"] == "halfyearly"
    assert body["mrr_inr"] == 1999
    t = await _tenant()
    days = (t.current_period_end - datetime.now(timezone.utc)).days
    assert 179 <= days <= 184
    async with async_session_factory() as db:
        inv = (await db.execute(select(Invoice).where(Invoice.tenant_id == t.id))).scalars().all()
    assert len(inv) == 1 and inv[0].amount_paise == 1999 * 6 * 100 and inv[0].cycle == "halfyearly"
    assert inv[0].period_end == t.current_period_end


async def test_renewal_stacks_after_paid_period_and_no_invoice_without_payment(client, shop) -> None:
    await client.patch(f"/control/api/tenants/{SLUG}", headers=CTL, json={"duration_months": 1})
    first_end = (await _tenant()).current_period_end
    r = await client.patch(f"/control/api/tenants/{SLUG}", headers=CTL, json={"duration_months": 12})
    assert r.status_code == 200 and r.json()["billing_cycle"] == "annual"
    t = await _tenant()
    assert t.current_period_end == plans.add_months(first_end, 12)
    async with async_session_factory() as db:
        n = (await db.execute(select(Invoice).where(Invoice.tenant_id == t.id))).scalars().all()
    assert n == []


async def test_bad_duration_rejected(client, shop) -> None:
    r = await client.patch(f"/control/api/tenants/{SLUG}", headers=CTL, json={"duration_months": 2})
    assert r.status_code == 400
    r = await client.get("/control/api/plan-catalog", headers=CTL)
    assert [d["months"] for d in r.json()["durations"]] == [1, 3, 6, 12]
