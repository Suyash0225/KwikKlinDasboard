"""Suyash ka apna control panel — 50-60 clients ek jagah se.

Ye tenant ke andar ka dashboard NAHI hai. Ye "software bechne wale" ka
panel hai: kaun sa shop kis plan par hai, kiska paisa aaya, kiska trial khatam
ho raha hai, kiska onboarding baaki hai.

Auth: `X-API-Key` (`ADMIN_API_KEY`) — ye endpoints sirf aapke liye hain,
kisi client user ke liye nahi. Isliye jaan-boojh kar session-login ke bahar
rakhe gaye hain: client ka user chahe kuch bhi ho, yahan nahi ghus sakta.
"""

from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    ROLE_OWNER,
    TENANT_ACTIVE,
    TENANT_CANCELLED,
    TENANT_READ_ONLY,
    TENANT_SUSPENDED,
    TENANT_TRIAL,
    BillingEvent,
    Tenant,
    User,
)
from app.routers.orders import require_admin_key
from app.services import auth, billing, plans

log = structlog.get_logger()

router = APIRouter(
    prefix="/control", tags=["control"], dependencies=[Depends(require_admin_key)]
)


def _out(t: Tenant, users: int = 0) -> dict:
    plan = plans.get(t.plan)
    now = datetime.now(timezone.utc)
    days_left = None
    if t.status == TENANT_TRIAL and t.trial_ends_at:
        days_left = (t.trial_ends_at - now).days
    elif t.current_period_end:
        days_left = (t.current_period_end - now).days
    return {
        "slug": t.slug,
        "shop_name": t.shop_name,
        "owner_name": t.owner_name,
        "owner_phone": t.owner_phone,
        "owner_email": t.owner_email,
        "city": t.city,
        "plan": plan.code,
        "plan_name": plan.name,
        "mrr_inr": plan.price_inr,
        "status": t.status,
        "days_left": days_left,
        "setup_fee_paid": t.setup_fee_paid,
        "onboarding_done": t.onboarding_done,
        "users": users,
        "notes": t.notes,
        "created_at": t.created_at.isoformat(),
    }


@router.get("/api/tenants")
async def list_tenants(db: AsyncSession = Depends(get_db)) -> dict:
    """Sab clients + ek line mein business ki sehat."""
    rows = (
        (await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))).scalars().all()
    )
    counts = dict(
        (
            await db.execute(select(User.tenant_id, func.count()).group_by(User.tenant_id))
        ).all()
    )
    out = [_out(t, counts.get(t.id, 0)) for t in rows]
    paying = [t for t in rows if t.status == TENANT_ACTIVE]
    mrr = sum(plans.get(t.plan).price_inr for t in paying)
    return {
        "tenants": out,
        "summary": {
            "total": len(rows),
            "active": len(paying),
            "trial": sum(1 for t in rows if t.status == TENANT_TRIAL),
            "past_due": sum(1 for t in rows if t.status not in (TENANT_ACTIVE, TENANT_TRIAL)),
            "onboarding_pending": sum(1 for t in rows if not t.onboarding_done),
            "mrr_inr": mrr,
            "arr_inr": mrr * 12,
        },
    }


class TenantCreateIn(BaseModel):
    """Aap khud client banate ho (phone par bika, form nahi bhara)."""

    shop_name: str = Field(min_length=2, max_length=120)
    owner_name: str = Field(min_length=2, max_length=120)
    phone: str
    email: str
    city: str | None = None
    plan: str = plans.DEFAULT_PLAN
    status: str = TENANT_TRIAL


@router.post("/api/tenants", status_code=201)
async def create_tenant(body: TenantCreateIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Client banao aur uska owner-login turant do.

    Temp password sirf is jawab mein dikhta hai — kahin store nahi hota,
    isliye ise wahin WhatsApp par bhej dena.
    """
    from app.routers.account import SignupIn, _slugify
    from app.utils.phone import normalize_phone

    try:
        phone = normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if (
        await db.execute(select(Tenant).where(Tenant.owner_phone == phone))
    ).scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Is number ka client pehle se hai")

    base = _slugify(body.shop_name)
    slug, n = base, 1
    while (
        await db.execute(select(Tenant.id).where(Tenant.slug == slug))
    ).scalar_one_or_none() is not None:
        n += 1
        slug = f"{base}-{n}"[:40]

    t = Tenant(
        slug=slug,
        shop_name=body.shop_name.strip(),
        owner_name=body.owner_name.strip(),
        owner_phone=phone,
        owner_email=body.email.strip().lower(),
        city=(body.city or "").strip() or None,
        plan=plans.get(body.plan).code,
        status=body.status,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=plans.TRIAL_DAYS),
    )
    db.add(t)
    await db.flush()
    temp = auth.temp_password()
    db.add(
        User(
            tenant_id=t.id,
            name=body.owner_name.strip(),
            email=body.email.strip().lower(),
            phone=phone,
            password_hash=auth.hash_password(temp),
            role=ROLE_OWNER,
            must_change_password=True,
        )
    )
    await db.commit()
    log.info("tenant_created_by_control", slug=slug, plan=t.plan)
    return {"tenant": _out(t, 1), "login_email": body.email.strip().lower(),
            "temp_password": temp}


class TenantPatchIn(BaseModel):
    plan: str | None = None
    status: str | None = None
    onboarding_done: bool | None = None
    notes: str | None = None
    extend_days: int | None = Field(default=None, ge=1, le=400)


@router.patch("/api/tenants/{slug}")
async def patch_tenant(
    slug: str, body: TenantPatchIn, db: AsyncSession = Depends(get_db)
) -> dict:
    t = (
        await db.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Client nahi mila")
    if body.plan:
        t.plan = plans.get(body.plan).code
    if body.status:
        allowed = (TENANT_TRIAL, TENANT_ACTIVE, TENANT_READ_ONLY, TENANT_SUSPENDED,
                   TENANT_CANCELLED)
        if body.status not in allowed:
            raise HTTPException(status_code=400, detail=f"status in mein se: {allowed}")
        t.status = body.status
    if body.onboarding_done is not None:
        t.onboarding_done = body.onboarding_done
    if body.notes is not None:
        t.notes = body.notes[:2000] or None
    if body.extend_days:
        base = max(t.current_period_end or datetime.now(timezone.utc),
                   datetime.now(timezone.utc))
        t.current_period_end = base + timedelta(days=body.extend_days)
        if t.status != TENANT_ACTIVE:
            t.status = TENANT_ACTIVE
    await db.commit()
    log.info("tenant_patched", slug=slug, plan=t.plan, status=t.status)
    return _out(t)


@router.post("/api/tenants/{slug}/reset-password")
async def reset_owner_password(slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Client password bhool gaya — naya temp password do."""
    t = (
        await db.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Client nahi mila")
    user = (
        await db.execute(
            select(User).where(User.tenant_id == t.id, User.role == ROLE_OWNER)
        )
    ).scalars().first()
    if user is None:
        raise HTTPException(status_code=404, detail="Owner user nahi mila")
    temp = auth.temp_password()
    user.password_hash = auth.hash_password(temp)
    user.must_change_password = True
    await db.commit()
    await auth.end_all_sessions(db, user.id)
    log.info("owner_password_reset", slug=slug)
    return {"email": user.email, "temp_password": temp}


@router.delete("/api/tenants/{slug}")
async def delete_tenant(
    slug: str, confirm: str = "", db: AsyncSession = Depends(get_db)
) -> dict:
    """Ek client ka account mita do (test signups saaf karne ke liye).

    Do taale, jaan-boojh kar:
    - `?confirm=<slug>` dena padta hai — galti se DELETE chal jaana mushkil ho
    - HOME tenant kabhi delete nahi hota, warna is dukaan ka apna dashboard
      hi anaath ho jaye

    Client ka BUSINESS data (orders/customers) alag deployment mein hota hai —
    yahan sirf uska account, users aur sessions jaate hain.
    """
    from app.models import BillingEvent, LoginSession, User as U
    from app.services import auth as auth_service

    t = (
        await db.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Client nahi mila")
    if confirm != slug:
        raise HTTPException(
            status_code=400,
            detail=f"Pakka karne ke liye ?confirm={slug} bhejein",
        )
    home = await auth_service.home_tenant(db)
    if home is not None and home.id == t.id:
        raise HTTPException(
            status_code=400,
            detail="Ye is deployment ki apni dukaan hai — delete nahi ho sakti.",
        )

    users = (await db.execute(select(U).where(U.tenant_id == t.id))).scalars().all()
    for u in users:
        for s_ in (
            await db.execute(select(LoginSession).where(LoginSession.user_id == u.id))
        ).scalars().all():
            await db.delete(s_)
        await db.delete(u)
    # paisa ka trail rehne do, bas tenant se de-link kar do
    for ev in (
        await db.execute(select(BillingEvent).where(BillingEvent.tenant_id == t.id))
    ).scalars().all():
        ev.tenant_id = None
    await db.delete(t)
    await db.commit()
    log.warning("tenant_deleted", slug=slug, users=len(users))
    return {"deleted": slug, "users_removed": len(users)}


@router.get("/api/billing/events")
async def billing_events(db: AsyncSession = Depends(get_db), limit: int = 50) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(BillingEvent).order_by(BillingEvent.at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for e in rows:
        t = await db.get(Tenant, e.tenant_id) if e.tenant_id else None
        out.append(
            {
                "at": e.at.isoformat(),
                "type": e.event_type,
                "tenant": t.slug if t else None,
                "amount_inr": (e.amount_paise or 0) / 100,
            }
        )
    return out


@router.post("/api/dunning/run")
async def run_dunning(db: AsyncSession = Depends(get_db)) -> dict:
    """Jinka paisa 7 din se ruka hai unhe read-only karo (data delete nahi)."""
    n = await billing.run_dunning(db)
    return {"moved_to_read_only": n}
