"""Dashboard KPIs — cached aggregates + daily snapshots for trends.

Design (Phase 1, thousands-of-tenants ready):
- compute() DB par har baar mat maro: 60s in-process TTL cache. Har tenant
  mutation (create/patch/delete/billing/sweep) invalidate() bulata hai —
  panel par MRR/counts "real time" dikhte hain bina har GET par 6 queries ke.
- Trends ke liye kpi_snapshots: roz raat ek JSONB row (nightly job). Trend
  = aaj vs ~30 din purana snapshot; snapshot na ho to trend null (kabhi
  fake nahi).
"""

import time
from datetime import date, datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import (
    TENANT_ACTIVE,
    TENANT_CANCELLED,
    TENANT_TRIAL,
    KpiSnapshot,
    Tenant,
)
from app.services import plans

log = structlog.get_logger()

_TTL_SECS = 60
_cache: dict | None = None
_cache_at: float = 0.0


def invalidate() -> None:
    """Kisi bhi tenant/billing mutation ke baad — agla read fresh hoga."""
    global _cache
    _cache = None


async def compute(db: AsyncSession) -> dict:
    """Saare KPIs ek shot mein (sirf tenants table — RLS-free, sasta)."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (
        await db.execute(
            select(
                Tenant.status, Tenant.plan, Tenant.deleted_at,
                Tenant.created_at, Tenant.onboarding_done,
            )
        )
    ).all()
    live = [r for r in rows if r.deleted_at is None]
    active = [r for r in live if r.status == TENANT_ACTIVE]
    mrr = sum(plans.get(r.plan).price_inr for r in active)
    return {
        "clients": len(live),
        "total": len(live),  # legacy alias — purane clients isi naam se padhte hain
        "onboarding_pending": sum(1 for r in live if not r.onboarding_done),
        "active": len(active),
        "trial": sum(1 for r in live if r.status == TENANT_TRIAL),
        "past_due": sum(
            1 for r in live if r.status not in (TENANT_ACTIVE, TENANT_TRIAL)
        ),
        "churned": sum(1 for r in rows if r.deleted_at is not None)
        + sum(1 for r in live if r.status == TENANT_CANCELLED),
        "new_this_month": sum(1 for r in live if r.created_at >= month_start),
        "mrr_inr": mrr,
        "arr_inr": mrr * 12,
    }


async def get(db: AsyncSession) -> dict:
    """Cached KPIs + trends. Panel yahi bulata hai."""
    global _cache, _cache_at
    if _cache is not None and (time.monotonic() - _cache_at) < _TTL_SECS:
        return _cache
    data = await compute(db)
    data["trends"] = await _trends(db, data)
    _cache, _cache_at = data, time.monotonic()
    return data


async def _trends(db: AsyncSession, today: dict) -> dict:
    """~30 din purane snapshot se % badlaav. Snapshot nahi -> null (honest)."""
    target = date.today() - timedelta(days=30)
    snap = (
        await db.execute(
            select(KpiSnapshot)
            .where(KpiSnapshot.at <= target)
            .order_by(KpiSnapshot.at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if snap is None:  # koi bhi purana snapshot chalega, warna null trends
        snap = (
            await db.execute(
                select(KpiSnapshot).order_by(KpiSnapshot.at.asc()).limit(1)
            )
        ).scalar_one_or_none()
    if snap is None or snap.at >= date.today():
        return {}
    out = {}
    for k in ("clients", "active", "mrr_inr", "past_due"):
        old = (snap.data or {}).get(k)
        if old is None:
            continue
        if old == 0:
            out[k] = None if today.get(k, 0) == 0 else 100.0
        else:
            out[k] = round((today.get(k, 0) - old) * 100.0 / old, 1)
    out["_since"] = snap.at.isoformat()
    return out


async def snapshot_today(db: AsyncSession) -> None:
    """Aaj ka snapshot (idempotent — ek din mein ek). Nightly job se."""
    today = date.today()
    exists = (
        await db.execute(select(KpiSnapshot.id).where(KpiSnapshot.at == today))
    ).scalar_one_or_none()
    if exists is not None:
        return
    db.add(KpiSnapshot(at=today, data=await compute(db)))
    await db.commit()
    log.info("kpi_snapshot_written", at=str(today))
