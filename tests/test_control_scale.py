"""Phase 1 proof: server-side pagination/search/sort, KPI cache+trends.

Kabhi saare rows load nahi hote; usage sirf page ke tenants ka; KPIs cached
hain aur mutation par turant fresh; trends snapshots se (guess se nahi).
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models.tenant import KpiSnapshot, Tenant
from app.services import kpis, tenant_context

# /control ka master key. VENDOR_API_KEY set ho to ADMIN_API_KEY wahan
# chalta hi NAHI (orders.vendor_master_key ka jaan-boojh kar rakha gaya
# niyam). Ye test seedha ADMIN_API_KEY bhejte the, isliye purane
# ek-dukaan wale .env par pass hote the aur alag vendor key wale par
# 401. Wahi helper use karo jo server use karta hai — dono soorat mein
# sahi.
from app.routers.orders import vendor_master_key

AUTH = {"X-API-Key": vendor_master_key()}
N = 5  # test tenants


@pytest.fixture(autouse=True)
async def _cleanup():
    kpis.invalidate()
    yield
    tenant_context.current_tenant_id.set(None)
    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM tenants WHERE slug LIKE 'test-scale-%'"))
        await db.execute(sqltext("DELETE FROM kpi_snapshots WHERE at < '2000-02-01'"))
        await db.commit()
    kpis.invalidate()


@pytest.fixture
async def many_tenants():
    async with async_session_factory() as db:
        base = datetime.now(timezone.utc)
        for i in range(N):
            db.add(Tenant(
                slug=f"test-scale-{i}", shop_name=f"Scale Shop {i}",
                owner_name=f"Owner {i}", owner_phone=f"+91999990006{i}",
                city="Varanasi" if i % 2 == 0 else "Lucknow",
                plan="starter" if i < 3 else "pro",
                status="trial" if i < 2 else "active",
                created_at=base - timedelta(minutes=i),
            ))
        await db.commit()
    kpis.invalidate()
    yield


async def test_cursor_pagination_no_overlap_no_gap(client, many_tenants) -> None:
    seen: list[str] = []
    cursor = ""
    for _ in range(10):
        ps = f"q=test-scale-&limit=2" + (f"&cursor={cursor}" if cursor else "")
        d = (await client.get(f"/control/api/tenants?{ps}", headers=AUTH)).json()
        assert len(d["tenants"]) <= 2
        seen += [t["slug"] for t in d["tenants"]]
        assert d["total"] == N
        if not d["next_cursor"]:
            break
        cursor = d["next_cursor"]
    assert len(seen) == N and len(set(seen)) == N, f"overlap/gap: {seen}"


async def test_search_filter_sort(client, many_tenants) -> None:
    # search by city
    d = (await client.get("/control/api/tenants?q=Lucknow", headers=AUTH)).json()
    assert {t["slug"] for t in d["tenants"]} == {"test-scale-1", "test-scale-3"}
    # filter by status+plan
    d = (
        await client.get("/control/api/tenants?q=test-scale-&status=active&plan=premium", headers=AUTH)
    ).json()
    assert {t["slug"] for t in d["tenants"]} == {"test-scale-3", "test-scale-4"}
    # sort by shop_name asc (offset mode)
    d = (
        await client.get(
            "/control/api/tenants?q=test-scale-&sort=shop_name&order=asc&limit=3", headers=AUTH
        )
    ).json()
    assert [t["slug"] for t in d["tenants"]] == ["test-scale-0", "test-scale-1", "test-scale-2"]
    assert d["next_offset"] == 3
    d2 = (
        await client.get(
            "/control/api/tenants?q=test-scale-&sort=shop_name&order=asc&limit=3&offset=3",
            headers=AUTH,
        )
    ).json()
    assert [t["slug"] for t in d2["tenants"]] == ["test-scale-3", "test-scale-4"]
    # invalid sort/cursor -> 400
    assert (
        await client.get("/control/api/tenants?sort=evil", headers=AUTH)
    ).status_code == 400
    assert (
        await client.get("/control/api/tenants?cursor=%%%", headers=AUTH)
    ).status_code == 400


async def test_kpis_cached_and_invalidated_on_mutation(client, many_tenants) -> None:
    k1 = (await client.get("/control/api/kpis", headers=AUTH)).json()
    assert k1["new_this_month"] >= N and "churned" in k1
    # cache hit: DB mein seedha badlav dikhna NAHI chahiye (60s TTL)...
    async with async_session_factory() as db:
        await db.execute(
            sqltext("UPDATE tenants SET status='active' WHERE slug='test-scale-0'")
        )
        await db.commit()
    k2 = (await client.get("/control/api/kpis", headers=AUTH)).json()
    assert k2["active"] == k1["active"], "cache bypass ho gaya?"
    # ...lekin control-API mutation invalidate karta hai -> fresh
    r = await client.patch(
        "/control/api/tenants/test-scale-1", headers=AUTH, json={"status": "active"}
    )
    assert r.status_code == 200
    k3 = (await client.get("/control/api/kpis", headers=AUTH)).json()
    assert k3["active"] == k1["active"] + 2  # dono flips ab dikhe


async def test_trends_come_from_snapshots(client, many_tenants) -> None:
    async with async_session_factory() as db:
        db.add(KpiSnapshot(
            at=date(2000, 1, 1),
            data={"clients": 1, "active": 1, "mrr_inr": 999, "past_due": 0},
        ))
        await db.commit()
    kpis.invalidate()
    k = (await client.get("/control/api/kpis", headers=AUTH)).json()
    tr = k["trends"]
    assert tr["_since"] == "2000-01-01"
    assert tr["clients"] == round((k["clients"] - 1) * 100.0 / 1, 1)


async def test_snapshot_today_idempotent() -> None:
    async with async_session_factory() as db:
        await kpis.snapshot_today(db)
        await kpis.snapshot_today(db)  # dobara — koi duplicate nahi
        n = (
            await db.execute(
                sqltext("SELECT count(*) FROM kpi_snapshots WHERE at = CURRENT_DATE")
            )
        ).scalar_one()
    assert n == 1
