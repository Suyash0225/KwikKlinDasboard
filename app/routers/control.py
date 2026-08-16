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
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    ROLE_OWNER,
    TENANT_ACTIVE,
    TENANT_CANCELLED,
    TENANT_LOCKED,
    TENANT_PAST_DUE,
    TENANT_SUSPENDED,
    TENANT_TRIAL,
    BillingEvent,
    Tenant,
    User,
)
from app.routers.orders import require_vendor_danger, require_vendor_key
from app.services import auth, billing, plans

log = structlog.get_logger()

router = APIRouter(
    prefix="/control", tags=["control"], dependencies=[Depends(require_vendor_key)]
)


def _phone_clash_detail(t: Tenant) -> str:
    """409 jo BATAYE number kiske paas hai aur aage kya karna hai —
    'pehle se hai' se kisi ko kuch samajh nahi aata."""
    if t.deleted_at is not None:
        return (
            f"Ye number RECYCLE BIN wale client '{t.shop_name}' ({t.slug}) ke "
            f"paas hai. Ya use ♻ restore karo, ya purge karke naya banao "
            f"(panel mein 'recycle bin' checkbox se dikhega)."
        )
    return f"Ye number '{t.shop_name}' ({t.slug}, {t.status}) ke paas pehle se hai."


def _out(t: Tenant, users: int = 0, usage: dict | None = None,
         agent_on: bool = True) -> dict:
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
        "trial_ends_at": t.trial_ends_at.isoformat() if t.trial_ends_at else None,
        "current_period_end": (
            t.current_period_end.isoformat() if t.current_period_end else None
        ),
        "wa_connected": bool(t.wa_phone_number_id),
        "billing_cycle": t.billing_cycle,
        "tags": t.tags or [],
        "deleted_at": t.deleted_at.isoformat() if t.deleted_at else None,
        # trial 7 din ke andar khatam -> panel warning dikhata hai
        "trial_warning": bool(
            t.status == TENANT_TRIAL and t.trial_ends_at
            and (t.trial_ends_at - now).days <= 7
        ),
        "limit_overrides": t.limit_overrides or {},
        "agent_enabled": agent_on,
        "credits": {"ai": t.ai_credits, "wa": t.wa_credits},
        "usage": (lambda _l: {
            "orders_month": (usage or {}).get("orders_month", 0),
            "orders_limit": _l["max_orders_month"],
            "wa_msgs_month": (usage or {}).get("wa_msgs_month", 0),
            "wa_limit": _l["whatsapp_message_limit"],
            "ai_calls_month": (usage or {}).get("ai_calls_month", 0),
            "ai_limit": _l["ai_usage_limit"],
            "staff": (usage or {}).get("staff", 0),
            "staff_limit": _l["max_staff"],
        })(plans.effective_limits(t)),
    }


@router.get("/api/kpis")
async def dashboard_kpis(db: AsyncSession = Depends(get_db)) -> dict:
    """Live KPIs (60s cache, mutation par invalidate) + 30-din trends."""
    from app.services import kpis

    return await kpis.get(db)


_SORTS = {
    "created_at": Tenant.created_at,
    "shop_name": Tenant.shop_name,
    "status": Tenant.status,
    "plan": Tenant.plan,
    "trial_ends_at": Tenant.trial_ends_at,
}


def _encode_cursor(t: Tenant) -> str:
    import base64

    return base64.urlsafe_b64encode(
        f"{t.created_at.isoformat()}|{t.id}".encode()
    ).decode()


def _decode_cursor(cur: str):
    import base64
    from datetime import datetime as _dt

    try:
        raw = base64.urlsafe_b64decode(cur.encode()).decode()
        ts, uid = raw.split("|", 1)
        return _dt.fromisoformat(ts), uid
    except Exception:
        raise HTTPException(status_code=400, detail="cursor kharab hai")


@router.get("/api/tenants")
async def list_tenants(
    q: str = "",
    status: str = "",
    plan: str = "",
    sort: str = "created_at",
    order: str = "desc",
    limit: int = 50,
    cursor: str = "",
    offset: int = 0,
    include_deleted: bool = False,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Server-side search/filter/sort + pagination — kabhi saare rows nahi.

    Default sort (created_at) par KEYSET cursor chalta hai (hazaaron tenants
    par bhi flat); doosre sorts par bounded limit/offset. Usage sirf isi
    page ke tenants ka compute hota hai.
    """
    limit = max(1, min(int(limit or 50), 100))
    if sort not in _SORTS:
        raise HTTPException(status_code=400, detail=f"sort in mein se: {list(_SORTS)}")
    desc = (order or "desc").lower() != "asc"

    base = select(Tenant)
    if not include_deleted:
        base = base.where(Tenant.deleted_at.is_(None))
    if status:
        base = base.where(Tenant.status == status)
    if plan:
        base = base.where(Tenant.plan == plans.get(plan).code)
    if q:
        like = f"%{q.strip()}%"
        base = base.where(
            Tenant.shop_name.ilike(like)
            | Tenant.slug.ilike(like)
            | Tenant.owner_name.ilike(like)
            | Tenant.owner_phone.ilike(like)
            | Tenant.city.ilike(like)
        )

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    col = _SORTS[sort]
    qy = base
    if sort == "created_at" and cursor:
        ts, uid = _decode_cursor(cursor)
        if desc:
            qy = qy.where(
                (Tenant.created_at < ts)
                | ((Tenant.created_at == ts) & (Tenant.id < uid))
            )
        else:
            qy = qy.where(
                (Tenant.created_at > ts)
                | ((Tenant.created_at == ts) & (Tenant.id > uid))
            )
    ordering = (
        [col.desc(), Tenant.id.desc()] if desc else [col.asc(), Tenant.id.asc()]
    )
    qy = qy.order_by(*ordering)
    if sort != "created_at":
        qy = qy.offset(max(0, int(offset or 0)))
    rows = (await db.execute(qy.limit(limit + 1))).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    ids = [t.id for t in rows]
    counts = dict(
        (
            await db.execute(
                select(User.tenant_id, func.count())
                .where(User.tenant_id.in_(ids))
                .group_by(User.tenant_id)
            )
        ).all()
    ) if ids else {}
    usage = await _usage_by_tenant(ids)
    agents = await _agent_flags(ids)

    from app.services import kpis as _kpis

    summary = await _kpis.get(db)
    return {
        "tenants": [
            _out(t, counts.get(t.id, 0), usage.get(t.id), agents.get(t.id, True))
            for t in rows
        ],
        "summary": summary,
        "total": total,
        "next_cursor": (
            _encode_cursor(rows[-1])
            if has_more and rows and sort == "created_at"
            else None
        ),
        "next_offset": (
            (int(offset or 0) + limit) if has_more and sort != "created_at" else None
        ),
    }



async def _agent_flags(ids: list) -> dict:
    """Har tenant ka agent_enabled switch, ek query mein (default True)."""
    if not ids:
        return {}
    from app.database import async_session_factory
    from app.models import SettingKV
    from app.services import tenant_context

    ctx = tenant_context.current_tenant_id.set(None)
    try:
        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    select(SettingKV.tenant_id, SettingKV.value).where(
                        SettingKV.key == "agent_enabled", SettingKV.tenant_id.in_(ids)
                    )
                )
            ).all()
    finally:
        tenant_context.current_tenant_id.reset(ctx)
    return {tid: bool((val or {}).get("v", True)) for tid, val in rows}


async def _usage_by_tenant(only_ids: list | None = None) -> dict:
    """Per-tenant is-mahine ke meters — orders / WA msgs / staff.

    Ye tables RLS-scoped hain aur request ctx home hota hai, isliye SYSTEM
    context ke apne session mein group-by karte hain (vendor ko sab dikhna
    legitimate hai — endpoint key-only hai)."""
    from app.database import async_session_factory
    from app.models import Conversation, LlmUsage, Order, Staff
    from app.models.enums import Direction
    from app.services import tenant_context

    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    ctx = tenant_context.current_tenant_id.set(None)
    try:
        async with async_session_factory() as db:
            oq = (
                select(Order.tenant_id, func.count())
                .where(Order.created_at >= month_start)
                .group_by(Order.tenant_id)
            )
            if only_ids is not None:
                oq = oq.where(Order.tenant_id.in_(only_ids))
            orders = dict((await db.execute(oq)).all())
            wq = (
                select(Conversation.tenant_id, func.count())
                .where(
                    Conversation.created_at >= month_start,
                    Conversation.direction == Direction.OUTBOUND,
                )
                .group_by(Conversation.tenant_id)
            )
            if only_ids is not None:
                wq = wq.where(Conversation.tenant_id.in_(only_ids))
            wa = dict((await db.execute(wq)).all())
            aq = (
                select(LlmUsage.tenant_id, func.count())
                .where(LlmUsage.at >= month_start)
                .group_by(LlmUsage.tenant_id)
            )
            if only_ids is not None:
                aq = aq.where(LlmUsage.tenant_id.in_(only_ids))
            ai = dict((await db.execute(aq)).all())
            sq = (
                select(Staff.tenant_id, func.count())
                .where(Staff.is_active.is_(True))
                .group_by(Staff.tenant_id)
            )
            if only_ids is not None:
                sq = sq.where(Staff.tenant_id.in_(only_ids))
            staff = dict((await db.execute(sq)).all())
    finally:
        tenant_context.current_tenant_id.reset(ctx)
    out: dict = {}
    for tid in set(orders) | set(wa) | set(staff) | set(ai):
        out[tid] = {
            "orders_month": orders.get(tid, 0),
            "wa_msgs_month": wa.get(tid, 0),
            "ai_calls_month": ai.get(tid, 0),
            "staff": staff.get(tid, 0),
        }
    return out


class TenantCreateIn(BaseModel):
    """Aap khud client banate ho (phone par bika, form nahi bhara)."""

    shop_name: str = Field(min_length=2, max_length=120)
    owner_name: str = Field(min_length=2, max_length=120)
    phone: str
    email: str
    city: str | None = None
    plan: str = plans.DEFAULT_PLAN
    status: str = TENANT_TRIAL
    slug: str | None = None            # khali = shop_name se auto
    cycle: str = "monthly"             # monthly | annual
    trial_days: int = Field(default=0, ge=0, le=60)  # 0 = default (7)


@router.post("/api/tenants", status_code=201)
async def create_tenant(
    body: TenantCreateIn, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Client banao — owner ko SET-PASSWORD INVITE LINK milta hai.

    Password na hum banate hain, na store karte hain, na bhejte hain —
    owner khud link par apna password set karta hai (services/invites.py).
    """
    from app.routers.account import _slugify
    from app.services import invites
    from app.utils.phone import normalize_phone

    try:
        phone = normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _clash = (
        await db.execute(select(Tenant).where(Tenant.owner_phone == phone))
    ).scalar_one_or_none()
    if _clash is not None:
        raise HTTPException(status_code=409, detail=_phone_clash_detail(_clash))
    if body.cycle not in ("monthly", "annual"):
        raise HTTPException(status_code=400, detail="cycle: monthly ya annual")

    if body.slug:
        slug = body.slug.strip().lower()
        import re as _re

        if not _re.fullmatch(r"[a-z0-9][a-z0-9-]{1,39}", slug):
            raise HTTPException(
                status_code=400,
                detail="slug: sirf a-z, 0-9 aur '-' (2-40 chars)",
            )
        if (
            await db.execute(select(Tenant.id).where(Tenant.slug == slug))
        ).scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="Ye slug pehle se liya hai")
    else:
        base = _slugify(body.shop_name)
        slug, n = base, 1
        while (
            await db.execute(select(Tenant.id).where(Tenant.slug == slug))
        ).scalar_one_or_none() is not None:
            n += 1
            slug = f"{base}-{n}"[:40]

    trial_days = body.trial_days or plans.TRIAL_DAYS
    t = Tenant(
        slug=slug,
        shop_name=body.shop_name.strip(),
        owner_name=body.owner_name.strip(),
        owner_phone=phone,
        owner_email=body.email.strip().lower(),
        city=(body.city or "").strip() or None,
        plan=plans.get(body.plan).code,
        status=body.status,
        billing_cycle=body.cycle,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=trial_days),
    )
    db.add(t)
    await db.flush()
    _user, invite_path = await invites.create_invite(
        db, tenant_id=t.id, email=body.email, name=body.owner_name,
        role=ROLE_OWNER, invited_by="control-panel", phone=phone,
    )
    log.info("tenant_created_by_control", slug=slug, plan=t.plan)
    from app.services import kpis as _kpis_inv

    _kpis_inv.invalidate()
    from app.services import audit

    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="tenant_created", tenant_id=t.id,
        args={"tenant": slug, "plan": t.plan, "cycle": body.cycle,
              "trial_days": trial_days,
              "ip": request.client.host if request.client else None},
    )
    # Best-effort WhatsApp invite (window/creds na hon to chup-chaap skip —
    # link response mein hai hi, operator khud bhej dega)
    try:
        from app.services.whatsapp import send_message

        await send_message(
            db, to_phone=phone,
            text=f"Namaste {body.owner_name}! KwikKlin par aapka account taiyar "
                 f"hai. Apna password yahan set karein: "
                 f"{str(request.base_url).rstrip('/')}{invite_path}",
            sent_by="system", enqueue_on_fail=False,
        )
        invite_sent = True
    except Exception:
        invite_sent = False
    return {
        "tenant": _out(t, 1),
        "login_email": body.email.strip().lower(),
        "invite_path": invite_path,
        "invite_sent_on_whatsapp": invite_sent,
    }


@router.get("/api/slug-check")
async def slug_check(slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Add-Tenant form ki live uniqueness check."""
    import re as _re

    slug = (slug or "").strip().lower()
    if not _re.fullmatch(r"[a-z0-9][a-z0-9-]{1,39}", slug):
        return {"slug": slug, "valid": False, "available": False}
    taken = (
        await db.execute(select(Tenant.id).where(Tenant.slug == slug))
    ).scalar_one_or_none() is not None
    return {"slug": slug, "valid": True, "available": not taken}


class TenantPatchIn(BaseModel):
    plan: str | None = None
    status: str | None = None
    onboarding_done: bool | None = None
    notes: str | None = None
    extend_days: int | None = Field(default=None, ge=1, le=400)
    # profile edits (Phase 2)
    shop_name: str | None = Field(default=None, min_length=2, max_length=120)
    owner_name: str | None = Field(default=None, min_length=2, max_length=120)
    owner_email: str | None = None
    owner_phone: str | None = None
    city: str | None = None
    billing_cycle: str | None = None
    tags: list[str] | None = None


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
        allowed = (TENANT_TRIAL, TENANT_ACTIVE, TENANT_PAST_DUE, TENANT_LOCKED, TENANT_SUSPENDED,
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
    # profile fields — before/after audit ke liye changes collect karo
    changes: dict = {}

    def _set(field: str, value) -> None:
        old_v = getattr(t, field)
        if value is not None and value != old_v:
            changes[field] = {"before": old_v, "after": value}
            setattr(t, field, value)

    _set("shop_name", body.shop_name and body.shop_name.strip())
    _set("owner_name", body.owner_name and body.owner_name.strip())
    _set("owner_email", body.owner_email and body.owner_email.strip().lower())
    _set("city", body.city and body.city.strip())
    if body.owner_phone:
        from app.utils.phone import normalize_phone as _np

        try:
            _set("owner_phone", _np(body.owner_phone))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if body.billing_cycle:
        if body.billing_cycle not in ("monthly", "annual"):
            raise HTTPException(status_code=400, detail="cycle: monthly ya annual")
        _set("billing_cycle", body.billing_cycle)
    if body.tags is not None:
        clean = sorted({tg.strip().lower()[:24] for tg in body.tags if tg.strip()})[:12]
        if clean != (t.tags or []):
            changes["tags"] = {"before": t.tags, "after": clean}
            t.tags = clean
    await db.commit()
    log.info("tenant_patched", slug=slug, plan=t.plan, status=t.status)
    from app.services import kpis as _kpis_inv

    _kpis_inv.invalidate()
    from app.services import audit

    await audit.record(
        actor_role="admin", actor="control-panel", action="tenant_updated", tenant_id=t.id,
        args={"tenant": slug, "plan": t.plan, "status": t.status,
              "changes": changes or None},
    )
    return _out(t)


@router.post(
    "/api/tenants/{slug}/reset-password",
    dependencies=[Depends(require_vendor_danger)],
)
async def reset_owner_password(
    slug: str, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Owner ka password-RESET LINK banao (temp password ab nahi banta).

    Saare sessions turant khatam; purana password tab tak chalta hai jab
    tak owner naya set nahi kar leta (lockout ka risk nahi)."""
    from app.services import invites

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
    await auth.end_all_sessions(db, user.id)
    _u, invite_path = await invites.create_invite(
        db, tenant_id=t.id, email=user.email, name=user.name,
        role=user.role, invited_by="control-panel", reset=True,
    )
    log.info("owner_password_reset", slug=slug)
    from app.services import audit

    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="owner_password_reset", tenant_id=t.id,
        args={"tenant": slug, "ip": request.client.host if request.client else None},
    )
    return {"email": user.email, "reset_path": invite_path}


@router.delete(
    "/api/tenants/{slug}", dependencies=[Depends(require_vendor_danger)]
)
async def delete_tenant(
    slug: str,
    request: Request,
    confirm: str = "",
    purge: bool = False,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """SOFT delete (recycle bin) — data salamat, list se gayab, logins band.

    Hard delete sirf tab: pehle soft-deleted ho AUR ?purge=true — recovery
    window ka matlab hi ye hai ki ek jhatke mein sab kuch nahi udta.
    Dono ke liye ?confirm=<slug> + danger-level key zaroori.
    HOME tenant kabhi delete nahi hota.
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
            status_code=400, detail=f"Pakka karne ke liye ?confirm={slug} bhejein"
        )
    home = await auth_service.home_tenant(db)
    if home is not None and home.id == t.id:
        raise HTTPException(
            status_code=400,
            detail="Ye is deployment ki apni dukaan hai — delete nahi ho sakti.",
        )
    ip = request.client.host if request.client else None
    from app.services import audit

    users = (await db.execute(select(U).where(U.tenant_id == t.id))).scalars().all()

    if purge:
        if t.deleted_at is None:
            raise HTTPException(
                status_code=400,
                detail="Pehle soft-delete karo — purge sirf recycle-bin wale "
                       "client par chalta hai (recovery window).",
            )
        for u in users:
            for s_ in (
                await db.execute(select(LoginSession).where(LoginSession.user_id == u.id))
            ).scalars().all():
                await db.delete(s_)
            await db.delete(u)
        from app.models import Invite as _Inv

        for inv in (
            await db.execute(select(_Inv).where(_Inv.tenant_id == t.id))
        ).scalars().all():
            await db.delete(inv)
        # paisa ka trail rehne do, bas tenant se de-link kar do
        for ev in (
            await db.execute(select(BillingEvent).where(BillingEvent.tenant_id == t.id))
        ).scalars().all():
            ev.tenant_id = None
        await db.delete(t)
        await db.commit()
        log.warning("tenant_purged", slug=slug, users=len(users))
        from app.services import kpis as _kpis_inv

        _kpis_inv.invalidate()
        await audit.record(
            actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
            action="tenant_purged",
            args={"tenant": slug, "users_removed": len(users), "ip": ip},
        )
        return {"purged": slug, "users_removed": len(users)}

    # soft delete
    t.deleted_at = datetime.now(timezone.utc)
    t.status = TENANT_CANCELLED
    for u in users:
        for s_ in (
            await db.execute(select(LoginSession).where(LoginSession.user_id == u.id))
        ).scalars().all():
            await db.delete(s_)
    await db.commit()
    log.warning("tenant_soft_deleted", slug=slug)
    from app.services import kpis as _kpis_inv

    _kpis_inv.invalidate()
    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="tenant_deleted", tenant_id=t.id,
        args={"tenant": slug, "soft": True, "ip": ip},
    )
    return {"deleted": slug, "soft": True, "recoverable": True}


@router.post("/api/tenants/{slug}/restore")
async def restore_tenant(
    slug: str, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Recycle bin se wapas — status past_due (read-only) par utarta hai,
    aap plan/paisa dekh kar active karo."""
    t = (
        await db.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if t is None or t.deleted_at is None:
        raise HTTPException(status_code=404, detail="Recycle bin mein aisa client nahi")
    t.deleted_at = None
    t.status = TENANT_PAST_DUE
    await db.commit()
    from app.services import kpis as _kpis_inv

    _kpis_inv.invalidate()
    from app.services import audit

    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="tenant_restored", tenant_id=t.id,
        args={"tenant": slug, "ip": request.client.host if request.client else None},
    )
    return _out(t)


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


@router.get("/api/audit")
async def control_audit(
    tenant_slug: str = "", action: str = "", limit: int = 100
) -> list[dict]:
    """SAARE tenants ka audit trail — kaun se tenant ne kya kiya.

    audit_log RLS-scoped hai; vendor ko cross-tenant dekhna hota hai,
    isliye system context (apna session, koi tenant GUC nahi) mein padhte
    hain. Ye endpoint key-only hai (require_vendor_key)."""
    from app.database import async_session_factory
    from app.models import AuditLog
    from app.services import tenant_context

    limit = max(1, min(int(limit or 100), 500))
    ctx = tenant_context.current_tenant_id.set(None)
    try:
        async with async_session_factory() as db:
            slug_by_id = {
                t.id: t.slug
                for t in (await db.execute(select(Tenant))).scalars().all()
            }
            q = select(AuditLog).order_by(AuditLog.at.desc()).limit(limit)
            if tenant_slug:
                tid = next(
                    (i for i, s in slug_by_id.items() if s == tenant_slug), None
                )
                if tid is None:
                    return []
                q = q.where(AuditLog.tenant_id == tid)
            if action:
                q = q.where(AuditLog.action == action)
            rows = (await db.execute(q)).scalars().all()
    finally:
        tenant_context.current_tenant_id.reset(ctx)
    return [
        {
            "at": r.at.isoformat(),
            "tenant": slug_by_id.get(r.tenant_id),
            "actor_role": r.actor_role,
            "actor": r.actor,
            "action": r.action,
            "args": r.args,
            "ok": r.ok,
        }
        for r in rows
    ]


@router.post("/api/backup/run")
async def control_backup_run() -> dict:
    """Abhi backup lo + restore-verify karo (nightly wala hi flow)."""
    from app.services.backup import run_backup, verify_backup

    path = await run_backup()
    if path is None:
        raise HTTPException(status_code=500, detail="backup fail hua — logs dekho")
    return {"file": path, "verified": await verify_backup(path)}


@router.post("/api/dunning/run")
async def run_dunning(db: AsyncSession = Depends(get_db)) -> dict:
    """Subscription sweep abhi chalao: expired trials/subscriptions ->
    past_due (read-only), 30-din grace khatam -> locked. Data delete NAHI."""
    moved = await billing.run_subscription_sweep(db)
    return {"moved": moved}


# ---------------------------------------------------------------------------
# Per-tenant user management (invite links — passwords kabhi nahi)
# ---------------------------------------------------------------------------


class UserInviteIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=160)
    role: str = "STAFF"


@router.get("/api/tenants/{slug}/users")
async def tenant_users(slug: str, db: AsyncSession = Depends(get_db)) -> list[dict]:
    from app.services.invites import PENDING_SENTINEL

    t = (
        await db.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Client nahi mila")
    rows = (
        await db.execute(
            select(User).where(User.tenant_id == t.id).order_by(User.created_at)
        )
    ).scalars().all()
    return [
        {
            "id": str(u.id),
            "name": u.name,
            "email": u.email,
            "role": u.role,
            "is_active": u.is_active,
            "pending_invite": u.password_hash == PENDING_SENTINEL,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        }
        for u in rows
    ]


@router.post("/api/tenants/{slug}/users/invite", status_code=201)
async def tenant_user_invite(
    slug: str, body: UserInviteIn, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    from app.models.tenant import ROLES
    from app.services import audit, invites

    if body.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"role in mein se: {ROLES}")
    t = (
        await db.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Client nahi mila")
    try:
        _u, invite_path = await invites.create_invite(
            db, tenant_id=t.id, email=body.email, name=body.name,
            role=body.role, invited_by="control-panel",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="user_invited", tenant_id=t.id,
        args={"tenant": slug, "email": body.email.lower(), "role": body.role,
              "ip": request.client.host if request.client else None},
    )
    return {"email": body.email.lower(), "role": body.role, "invite_path": invite_path}


@router.post("/api/tenants/{slug}/users/{user_id}/revoke")
async def tenant_user_revoke(
    slug: str, user_id: str, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    from app.services import audit

    t = (
        await db.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Client nahi mila")
    u = await db.get(User, user_id)
    if u is None or u.tenant_id != t.id:
        raise HTTPException(status_code=404, detail="User nahi mila")
    owners_left = (
        await db.execute(
            select(func.count()).select_from(User).where(
                User.tenant_id == t.id, User.role == ROLE_OWNER,
                User.is_active.is_(True), User.id != u.id,
            )
        )
    ).scalar_one()
    if u.role == ROLE_OWNER and owners_left == 0:
        raise HTTPException(status_code=400, detail="Aakhri OWNER revoke nahi ho sakta")
    u.is_active = False
    await db.commit()
    await auth.end_all_sessions(db, u.id)
    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="user_revoked", tenant_id=t.id,
        args={"tenant": slug, "email": u.email,
              "ip": request.client.host if request.client else None},
    )
    return {"revoked": u.email}


# ---------------------------------------------------------------------------
# Per-admin keys (rotation) — danger level required to manage
# ---------------------------------------------------------------------------


class AdminKeyIn(BaseModel):
    label: str = Field(min_length=2, max_length=60)
    level: str = "write"  # read | write | danger


@router.get("/api/admin-keys", dependencies=[Depends(require_vendor_danger)])
async def admin_keys_list(db: AsyncSession = Depends(get_db)) -> list[dict]:
    from app.models import AdminKey

    rows = (
        await db.execute(select(AdminKey).order_by(AdminKey.created_at.desc()))
    ).scalars().all()
    return [
        {
            "id": str(k.id),
            "label": k.label,
            "level": k.level,
            "created_at": k.created_at.isoformat(),
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "revoked": k.revoked_at is not None,
        }
        for k in rows
    ]


@router.post(
    "/api/admin-keys", status_code=201,
    dependencies=[Depends(require_vendor_danger)],
)
async def admin_keys_create(
    body: AdminKeyIn, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Nayi per-admin key. RAW KEY SIRF IS RESPONSE MEIN — DB mein hash."""
    import hashlib as _hl
    import secrets as _sec

    from app.models import AdminKey
    from app.services import audit

    if body.level not in ("read", "write", "danger"):
        raise HTTPException(status_code=400, detail="level: read | write | danger")
    raw = "kk_adm_" + _sec.token_urlsafe(24)
    db.add(
        AdminKey(
            label=body.label.strip(),
            key_hash=_hl.sha256(raw.encode()).hexdigest(),
            level=body.level,
        )
    )
    await db.commit()
    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="admin_key_created",
        args={"label": body.label, "level": body.level,
              "ip": request.client.host if request.client else None},
    )
    return {"label": body.label, "level": body.level, "key": raw}


@router.post(
    "/api/admin-keys/{key_id}/revoke",
    dependencies=[Depends(require_vendor_danger)],
)
async def admin_keys_revoke(
    key_id: str, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    from app.models import AdminKey
    from app.services import audit

    k = await db.get(AdminKey, key_id)
    if k is None:
        raise HTTPException(status_code=404, detail="Key nahi mili")
    k.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="admin_key_revoked",
        args={"label": k.label,
              "ip": request.client.host if request.client else None},
    )
    return {"revoked": k.label}


# ---------------------------------------------------------------------------
# Phase 2: Client profile — detail, timeline, export/import
# ---------------------------------------------------------------------------


async def _tenant_or_404(db: AsyncSession, slug: str) -> Tenant:
    t = (
        await db.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Client nahi mila")
    return t


@router.get("/api/tenants/{slug}")
async def tenant_detail(slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Profile page ka ek-shot payload: profile + subscription + usage +
    users-count + last invoices. Secrets (wa_token) KABHI nahi jaate."""
    from app.models import Invoice

    t = await _tenant_or_404(db, slug)
    users_count = (
        await db.execute(
            select(func.count()).select_from(User).where(User.tenant_id == t.id)
        )
    ).scalar_one()
    usage = await _usage_by_tenant([t.id])
    agents = await _agent_flags([t.id])
    out = _out(t, users_count, usage.get(t.id), agents.get(t.id, True))
    out["subscription"] = {
        "status": t.status,
        "billing_cycle": t.billing_cycle,
        "trial_ends_at": out["trial_ends_at"],
        "current_period_end": out["current_period_end"],
        "setup_fee_paid": t.setup_fee_paid,
        "rzp_subscription_id": t.rzp_subscription_id,
        "wa_phone_number_id": t.wa_phone_number_id,
    }
    invs = (
        await db.execute(
            select(Invoice)
            .where(Invoice.tenant_id == t.id)
            .order_by(Invoice.created_at.desc())
            .limit(10)
        )
    ).scalars().all()
    out["invoices"] = [
        {
            "date": i.created_at.isoformat(),
            "payment_id": i.rzp_payment_id,
            "plan": plans.get(i.plan).name,
            "cycle": i.cycle,
            "amount_inr": round(i.amount_paise / 100, 2),
            "status": i.status,
        }
        for i in invs
    ]
    # WA overage estimate (billing ke liye): limit ke upar ke msgs x rate.
    from app.services import app_settings

    try:
        rate_paise = int(await app_settings.get(db, "wa_overage_paise_per_msg"))
    except Exception:
        rate_paise = 0
    u = out["usage"]
    over = max(0, (u["wa_msgs_month"] or 0) - (u["wa_limit"] or 10**9)) if u["wa_limit"] is not None else 0
    out["billing_estimate"] = {
        "wa_overage_msgs": over,
        "rate_paise_per_msg": rate_paise,
        "wa_overage_inr": round(over * rate_paise / 100, 2),
        "note": "rate app-settings 'wa_overage_paise_per_msg' se; 0 = overage billing off",
    }
    return out


@router.get("/api/tenants/{slug}/timeline")
async def tenant_timeline(
    slug: str, limit: int = 50, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    """Activity timeline: audit + billing events + invoices, merged desc.

    audit_log RLS-scoped hai -> system-context read (route vendor key-only
    hai, isliye cross-tenant read legitimate hai)."""
    from app.database import async_session_factory
    from app.models import AuditLog, Invoice
    from app.services import tenant_context

    t = await _tenant_or_404(db, slug)
    limit = max(1, min(int(limit or 50), 200))
    items: list[dict] = []

    ctx = tenant_context.current_tenant_id.set(None)
    try:
        async with async_session_factory() as sdb:
            for a in (
                await sdb.execute(
                    select(AuditLog)
                    .where(AuditLog.tenant_id == t.id)
                    .order_by(AuditLog.at.desc())
                    .limit(limit)
                )
            ).scalars().all():
                items.append({
                    "at": a.at.isoformat(), "kind": "audit",
                    "title": a.action, "actor": a.actor or a.actor_role,
                    "detail": a.args, "ok": a.ok,
                })
    finally:
        tenant_context.current_tenant_id.reset(ctx)

    for b in (
        await db.execute(
            select(BillingEvent)
            .where(BillingEvent.tenant_id == t.id)
            .order_by(BillingEvent.at.desc())
            .limit(limit)
        )
    ).scalars().all():
        items.append({
            "at": b.at.isoformat(), "kind": "billing",
            "title": b.event_type, "actor": "billing",
            "detail": {"amount_inr": (b.amount_paise or 0) / 100 or None},
            "ok": True,
        })
    for i in (
        await db.execute(
            select(Invoice)
            .where(Invoice.tenant_id == t.id)
            .order_by(Invoice.created_at.desc())
            .limit(limit)
        )
    ).scalars().all():
        items.append({
            "at": i.created_at.isoformat(), "kind": "invoice",
            "title": f"invoice {i.status}", "actor": "billing",
            "detail": {"amount_inr": round(i.amount_paise / 100, 2),
                       "payment_id": i.rzp_payment_id}, "ok": True,
        })
    items.sort(key=lambda x: x["at"], reverse=True)
    return items[:limit]


@router.get("/api/tenants/{slug}/export")
async def tenant_export(
    slug: str,
    request: Request,
    format: str = "json",
    include_business: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Per-client export/backup. STRICT tenant isolation: SIRF isi tenant ka
    data — RLS-scoped tables system-context mein explicitly tenant_id se
    filter hote hain. Password-hash/tokens kabhi export nahi hote."""
    from fastapi.responses import Response as _Resp

    from app.database import async_session_factory
    from app.models import Invoice
    from app.services import audit as _audit
    from app.services import tenant_context

    t = await _tenant_or_404(db, slug)
    users = (
        await db.execute(select(User).where(User.tenant_id == t.id))
    ).scalars().all()
    data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tenant": {
            "slug": t.slug, "shop_name": t.shop_name, "owner_name": t.owner_name,
            "owner_phone": t.owner_phone, "owner_email": t.owner_email,
            "city": t.city, "plan": t.plan, "billing_cycle": t.billing_cycle,
            "status": t.status, "tags": t.tags or [], "notes": t.notes,
            "created_at": t.created_at.isoformat(),
        },
        "users": [
            {"name": u.name, "email": u.email, "phone": u.phone, "role": u.role,
             "is_active": u.is_active}
            for u in users
        ],
        "invoices": [
            {"date": i.created_at.isoformat(), "payment_id": i.rzp_payment_id,
             "plan": i.plan, "cycle": i.cycle, "amount_paise": i.amount_paise,
             "status": i.status}
            for i in (
                await db.execute(select(Invoice).where(Invoice.tenant_id == t.id))
            ).scalars().all()
        ],
    }
    if include_business:
        ctx = tenant_context.current_tenant_id.set(None)
        try:
            async with async_session_factory() as sdb:
                from app.models import Customer, Order, Payment

                data["business"] = {
                    "customers": [
                        {"phone": c.phone, "name": c.name, "address": c.address,
                         "created_at": c.created_at.isoformat()}
                        for c in (
                            await sdb.execute(
                                select(Customer).where(Customer.tenant_id == t.id)
                            )
                        ).scalars().all()
                    ],
                    "orders": [
                        {"order_number": o.order_number, "status": o.status.name,
                         "total_amount": str(o.total_amount or 0),
                         "amount_paid": str(o.amount_paid or 0),
                         "created_at": o.created_at.isoformat()}
                        for o in (
                            await sdb.execute(
                                select(Order).where(Order.tenant_id == t.id)
                            )
                        ).scalars().all()
                    ],
                    "payments": [
                        {"amount": str(pm.amount), "method": pm.method.name,
                         "received_at": pm.received_at.isoformat()}
                        for pm in (
                            await sdb.execute(
                                select(Payment).where(Payment.tenant_id == t.id)
                            )
                        ).scalars().all()
                    ],
                }
        finally:
            tenant_context.current_tenant_id.reset(ctx)

    await _audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="tenant_exported", tenant_id=t.id,
        args={"tenant": slug, "format": format, "business": include_business,
              "ip": request.client.host if request.client else None},
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    if format == "csv":
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["slug", "shop_name", "owner_name", "owner_phone", "owner_email",
                    "city", "plan", "cycle", "status", "tags"])
        tn = data["tenant"]
        w.writerow([tn["slug"], tn["shop_name"], tn["owner_name"], tn["owner_phone"],
                    tn["owner_email"], tn["city"], tn["plan"], tn["billing_cycle"],
                    tn["status"], "|".join(tn["tags"])])
        w.writerow([])
        w.writerow(["user_name", "email", "phone", "role", "active"])
        for u in data["users"]:
            w.writerow([u["name"], u["email"], u["phone"], u["role"], u["is_active"]])
        return _Resp(
            content=buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="{slug}-{stamp}.csv"'},
        )
    import json as _json

    return _Resp(
        content=_json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="{slug}-{stamp}.json"'},
    )


class TenantImportIn(BaseModel):
    tenant: dict
    users: list[dict] = Field(default_factory=list)


@router.post(
    "/api/tenants/import", status_code=201,
    dependencies=[Depends(require_vendor_danger)],
)
async def tenant_import(
    body: TenantImportIn, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Export JSON se client wapas banao (profile + users-as-invites).

    Business data import NAHI hota — wo pg_restore/onboarding ka kaam hai.
    Slug clash ho to -2/-3 suffix. Users pending-invite bante hain
    (passwords import mein hote hi nahi — export unhe nikalta hi nahi)."""
    from app.routers.account import _slugify
    from app.services import invites
    from app.utils.phone import normalize_phone

    tn = body.tenant or {}
    for req in ("shop_name", "owner_name", "owner_phone"):
        if not tn.get(req):
            raise HTTPException(status_code=400, detail=f"tenant.{req} chahiye")
    try:
        phone = normalize_phone(tn["owner_phone"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _clash = (
        await db.execute(select(Tenant).where(Tenant.owner_phone == phone))
    ).scalar_one_or_none()
    if _clash is not None:
        raise HTTPException(status_code=409, detail=_phone_clash_detail(_clash))

    base = _slugify(tn.get("slug") or tn["shop_name"])
    slug, n = base, 1
    while (
        await db.execute(select(Tenant.id).where(Tenant.slug == slug))
    ).scalar_one_or_none() is not None:
        n += 1
        slug = f"{base}-{n}"[:40]
    t = Tenant(
        slug=slug, shop_name=tn["shop_name"][:120], owner_name=tn["owner_name"][:120],
        owner_phone=phone, owner_email=(tn.get("owner_email") or "").lower() or None,
        city=tn.get("city"), plan=plans.get(tn.get("plan") or "starter").code,
        billing_cycle=tn.get("billing_cycle") or "monthly",
        status=TENANT_TRIAL,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=plans.TRIAL_DAYS),
        tags=tn.get("tags") or [], notes=tn.get("notes"),
    )
    db.add(t)
    await db.flush()
    invite_paths = []
    for u in body.users[:20]:
        if not u.get("email"):
            continue
        try:
            _usr, path = await invites.create_invite(
                db, tenant_id=t.id, email=u["email"],
                name=u.get("name") or u["email"],
                role=u.get("role") or "STAFF", invited_by="import",
                phone=u.get("phone"),
            )
            invite_paths.append({"email": u["email"].lower(), "invite_path": path})
        except ValueError:
            continue
    if not invite_paths:  # kam se kam owner ka invite
        _usr, path = await invites.create_invite(
            db, tenant_id=t.id,
            email=tn.get("owner_email") or f"owner@{slug}.local",
            name=tn["owner_name"], role=ROLE_OWNER, invited_by="import", phone=phone,
        )
        invite_paths.append({"email": (tn.get("owner_email") or "").lower(),
                             "invite_path": path})
    from app.services import audit as _audit
    from app.services import kpis as _kpis_inv

    _kpis_inv.invalidate()
    await _audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="tenant_imported", tenant_id=t.id,
        args={"tenant": slug, "users": len(invite_paths),
              "ip": request.client.host if request.client else None},
    )
    return {"tenant": _out(t, len(invite_paths)), "invites": invite_paths}


# ---------------------------------------------------------------------------
# Client Ops: login-as-client, WhatsApp creds, AI toggle, limit overrides
# ---------------------------------------------------------------------------


@router.post(
    "/api/tenants/{slug}/impersonate",
    dependencies=[Depends(require_vendor_danger)],
)
async def impersonate_tenant(
    slug: str, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Client ke OWNER ki tarah login (testing/support) — 30 MINUTE ka
    session, audit-logged. Link kholte hi us tenant ka dashboard."""
    t = await _tenant_or_404(db, slug)
    user = (
        await db.execute(
            select(User).where(
                User.tenant_id == t.id, User.role == ROLE_OWNER,
                User.is_active.is_(True),
            )
        )
    ).scalars().first()
    if user is None:
        raise HTTPException(status_code=404, detail="Active owner user nahi mila")
    token = await auth.start_session(
        db, user, ip=request.client.host if request.client else "",
        user_agent="impersonation", minutes=30,
    )
    from app.services import audit

    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="impersonation", tenant_id=t.id,
        args={"tenant": slug, "as_user": user.email, "ttl_min": 30,
              "ip": request.client.host if request.client else None},
    )
    return {"adopt_path": f"/api/session/adopt/{token}", "expires_in_min": 30,
            "as_user": user.email}


class WaSetIn(BaseModel):
    phone_number_id: str = Field(min_length=5, max_length=30)
    token: str = Field(min_length=20)
    waba_id: str = ""


@router.post("/api/tenants/{slug}/whatsapp")
async def set_tenant_whatsapp(
    slug: str, body: WaSetIn, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Vendor client ke WhatsApp creds panel se jode — Graph par live
    validate, duplicate number 409, token store hota hai par kabhi wapas
    nahi jaata (masked)."""
    from app.services import whatsapp

    pnid = body.phone_number_id.strip()
    if not await whatsapp.validate_credentials(pnid, body.token.strip()):
        raise HTTPException(
            status_code=400,
            detail="Meta ne creds reject kiye — phone_number_id/token check karo",
        )
    t = await _tenant_or_404(db, slug)
    dupe = (
        await db.execute(
            select(Tenant).where(
                Tenant.wa_phone_number_id == pnid, Tenant.id != t.id
            )
        )
    ).scalar_one_or_none()
    if dupe is not None:
        raise HTTPException(
            status_code=409, detail=f"Ye number '{dupe.shop_name}' se juda hai"
        )
    t.wa_phone_number_id = pnid
    t.wa_waba_id = body.waba_id.strip() or None
    t.wa_token = body.token.strip()
    await db.commit()
    from app.services import audit

    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="whatsapp_connect", tenant_id=t.id,
        args={"tenant": slug, "phone_number_id": pnid, "via": "control-panel",
              "ip": request.client.host if request.client else None},
    )
    return {"connected": True, "phone_number_id": pnid}


class TenantSettingIn(BaseModel):
    key: str
    value: object = None


# Vendor panel se sirf ye settings chhoo sakte hain — poora settings-surface
# client ke apne dashboard ka hai.
_CONTROL_SETTINGS = ("agent_enabled", "marketing_autonomy", "llm_monthly_budget_usd")


@router.put("/api/tenants/{slug}/settings")
async def set_tenant_setting(
    slug: str, body: TenantSettingIn, request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Client ki AI/agent settings vendor panel se (jaise agent on/off).

    settings_kv per-tenant hai — yahan USI tenant ke context mein likhte
    hain taaki uski row bane/badle, home ki nahi."""
    if body.key not in _CONTROL_SETTINGS:
        raise HTTPException(
            status_code=400, detail=f"key in mein se: {_CONTROL_SETTINGS}"
        )
    t = await _tenant_or_404(db, slug)
    from app.database import async_session_factory
    from app.services import app_settings, tenant_context

    ctx = tenant_context.current_tenant_id.set(t.id)
    try:
        async with async_session_factory() as tdb:
            await app_settings.set_value(tdb, body.key, body.value)
            new_val = await app_settings.get(tdb, body.key)
    finally:
        tenant_context.current_tenant_id.reset(ctx)
    from app.services import audit

    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="tenant_setting_changed", tenant_id=t.id,
        args={"tenant": slug, "key": body.key, "value": body.value,
              "ip": request.client.host if request.client else None},
    )
    return {"key": body.key, "value": new_val}


class LimitOverridesIn(BaseModel):
    ai_usage_limit: int | None = None
    whatsapp_message_limit: int | None = None
    max_orders_month: int | None = None
    max_staff: int | None = None
    # marketing sabse mehngi cheez hai — iska cap bhi per-client badla ja sake
    max_campaign_msgs_month: int | None = None
    clear: bool = False  # True = saare overrides hatao (plan defaults wapas)


@router.put("/api/tenants/{slug}/limits")
async def set_tenant_limits(
    slug: str, body: LimitOverridesIn, request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Per-client limit overrides (plan ke upar vendor ka haath).

    Value -1 = unlimited; field bhejo hi mat to untouched; clear=true =
    sab overrides hatao. Enforcement turant (plans.effective_limits)."""
    t = await _tenant_or_404(db, slug)
    if body.clear:
        t.limit_overrides = {}
    else:
        cur = dict(t.limit_overrides or {})
        for k in plans.OVERRIDABLE_LIMITS:
            v = getattr(body, k)
            if v is not None:
                cur[k] = v
        t.limit_overrides = cur
    await db.commit()
    from app.services import audit, kpis as _kpis

    _kpis.invalidate()
    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="limits_overridden", tenant_id=t.id,
        args={"tenant": slug, "overrides": t.limit_overrides,
              "ip": request.client.host if request.client else None},
    )
    return {"limit_overrides": t.limit_overrides,
            "effective": plans.effective_limits(t)}


# ---------------------------------------------------------------------------
# Vendor session — key browser storage se bahar (httpOnly cookie)
# ---------------------------------------------------------------------------


@router.post("/api/session")
async def open_vendor_session(request: Request, response: Response) -> dict:
    """Key EK BAAR bhejo (X-API-Key header), badle mein httpOnly cookie.

    Verification wahi purana `require_vendor_key` karta hai (router-level
    dependency) — permission model bilkul nahi badla, sirf key ab browser
    mein kahin persist nahi hoti. Cookie: httpOnly (JS padh hi nahi sakta,
    XSS ke baad bhi), SameSite=Strict (CSRF), Secure in production,
    path=/control (baaki app ko kabhi nahi jaati), 8 ghante.
    """
    from app.routers.orders import (
        VENDOR_COOKIE,
        VENDOR_SESSION_HOURS,
        mint_vendor_token,
    )

    level = getattr(request.state, "vendor_level", "read")
    label = getattr(request.state, "vendor_label", "env-key")
    kid = getattr(request.state, "vendor_kid", "env")
    response.set_cookie(
        VENDOR_COOKIE,
        mint_vendor_token(level, label, kid),
        max_age=VENDOR_SESSION_HOURS * 3600,
        httponly=True,
        samesite="strict",
        secure=settings.ENVIRONMENT == "production",
        path="/control",
    )
    log.info("vendor_session_opened", label=label, level=level)
    return {"level": level, "label": label, "expires_in_h": VENDOR_SESSION_HOURS}


@router.get("/api/session")
async def vendor_session_info(request: Request) -> dict:
    """Panel boot par: cookie zinda hai? (level UI ko batata hai kya dikhana)"""
    return {
        "level": getattr(request.state, "vendor_level", "read"),
        "label": getattr(request.state, "vendor_label", "env-key"),
        "via": getattr(request.state, "vendor_via", "header"),
    }


@router.post("/api/session/logout")
async def close_vendor_session(response: Response) -> dict:
    from app.routers.orders import VENDOR_COOKIE

    response.delete_cookie(VENDOR_COOKIE, path="/control")
    return {"ok": True}


class SetPasswordIn(BaseModel):
    # 8+ isliye ki auth.hash_password khud bhi yahi minimum maangta hai
    password: str = Field(min_length=8, max_length=128)


@router.post(
    "/api/tenants/{slug}/set-password",
    dependencies=[Depends(require_vendor_danger)],
)
async def set_owner_password(
    slug: str, body: SetPasswordIn, request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Support ka seedha rasta: owner ka password aap khud set kar do.

    Link wala flow (reset-password) ab bhi hai — par jab client phone par
    kehta hai "naya password bana ke bhej do", tab ye kaam aata hai.

    Niyam wahi rehte hain: password DB mein sirf scrypt-hash jaata hai
    (kabhi plaintext nahi, logs mein bhi nahi), saare purane sessions
    turant khatam, aur user ko pehle login par khud badalna padta hai.
    """
    t = await _tenant_or_404(db, slug)
    user = (
        await db.execute(
            select(User).where(
                User.tenant_id == t.id, User.role == ROLE_OWNER,
                User.is_active.is_(True),
            )
        )
    ).scalars().first()
    if user is None:
        raise HTTPException(status_code=404, detail="Active owner user nahi mila")
    try:
        user.password_hash = auth.hash_password(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    user.must_change_password = True
    await db.commit()
    await auth.end_all_sessions(db, user.id)
    from app.services import audit

    await audit.record(
        actor_role="admin", actor=getattr(request.state, "vendor_label", "control"),
        action="owner_password_set", tenant_id=t.id,
        # NOTE: password kabhi args mein nahi jaata.
        args={"tenant": slug, "email": user.email, "by": "control-panel",
              "ip": request.client.host if request.client else None},
    )
    log.info("owner_password_set", slug=slug, email=user.email)
    return {"ok": True, "email": user.email,
            "must_change_password": True,
            "sessions_ended": True}


# ---------------------------------------------------------------------------
# Recharge: plan limit ke upar ka top-up (AI calls / WhatsApp messages)
# ---------------------------------------------------------------------------


class RechargeIn(BaseModel):
    kind: str = Field(pattern="^(ai|wa)$")
    # +add, -remove. 0 bekaar hai; upper bound galti se 10 lakh na ho jaye.
    amount: int = Field(ge=-1_000_000, le=1_000_000)
    reason: str = Field(default="", max_length=200)
    # Kitne din chalega ye top-up. None/0 = kabhi khatam nahi (default).
    valid_days: int | None = Field(default=None, ge=1, le=3650)


@router.post("/api/tenants/{slug}/credits")
async def recharge_tenant(
    slug: str, body: RechargeIn, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Client ka recharge: AI calls ya WhatsApp messages ka top-up.

    Balance tenants par rehta hai (quota hot-path fast), lekin har badlaav
    credit_ledger mein bhi jaata hai — "kab kitna daala/kaata" ka poora
    trail. Negative amount = wapas lena (balance kabhi 0 se neeche nahi)."""
    from app.models import CreditLedger

    if body.amount == 0:
        raise HTTPException(status_code=400, detail="amount 0 nahi ho sakta")
    t = await _tenant_or_404(db, slug)
    col = "ai_credits" if body.kind == "ai" else "wa_credits"
    before = getattr(t, col)
    after = max(0, before + body.amount)
    setattr(t, col, after)
    who = getattr(request.state, "vendor_label", "control")
    expires_at = None
    if body.valid_days and body.amount > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.valid_days)
    db.add(
        CreditLedger(
            tenant_id=t.id, kind=body.kind, amount=after - before,
            balance_after=after, reason=body.reason.strip() or None, created_by=who,
            expires_at=expires_at,
        )
    )
    await db.commit()
    from app.services import audit

    await audit.record(
        actor_role="admin", actor=who, action="credits_recharged", tenant_id=t.id,
        args={"tenant": slug, "kind": body.kind, "amount": after - before,
              "balance": after, "reason": body.reason.strip() or None,
              "ip": request.client.host if request.client else None},
    )
    log.info("credits_recharged", tenant=slug, kind=body.kind, amount=after - before,
             balance=after)
    return {"kind": body.kind, "balance": after, "changed_by": after - before,
            "expires_at": expires_at.isoformat() if expires_at else None}


@router.get("/api/tenants/{slug}/credits")
async def credit_history(
    slug: str, limit: int = 30, db: AsyncSession = Depends(get_db)
) -> dict:
    from app.models import CreditLedger

    t = await _tenant_or_404(db, slug)
    rows = (
        await db.execute(
            select(CreditLedger)
            .where(CreditLedger.tenant_id == t.id)
            .order_by(CreditLedger.at.desc())
            .limit(max(1, min(int(limit or 30), 100)))
        )
    ).scalars().all()
    return {
        "ai_credits": t.ai_credits,
        "wa_credits": t.wa_credits,
        "history": [
            {"at": r.at.isoformat(), "kind": r.kind, "amount": r.amount,
             "balance_after": r.balance_after, "reason": r.reason, "by": r.created_by,
             "expires_at": r.expires_at.isoformat() if r.expires_at else None,
             "expired": r.expired_at is not None}
            for r in rows
        ],
    }


@router.get("/api/recharge-requests")
async def list_recharge_requests(
    status: str = "pending", limit: int = 50, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    """Clients ne kya maanga — panel ka inbox."""
    from app.models import RechargeRequest

    q = select(RechargeRequest).order_by(RechargeRequest.created_at.desc())
    if status:
        q = q.where(RechargeRequest.status == status)
    rows = (await db.execute(q.limit(max(1, min(int(limit or 50), 200))))).scalars().all()
    slugs = {
        t.id: (t.slug, t.shop_name)
        for t in (await db.execute(select(Tenant))).scalars().all()
    }
    return [
        {
            "id": str(r.id), "at": r.created_at.isoformat(),
            "tenant": slugs.get(r.tenant_id, ("?", "?"))[0],
            "shop": slugs.get(r.tenant_id, ("?", "?"))[1],
            "pack": r.pack, "kind": r.kind, "units": r.units,
            "amount_inr": r.amount_inr, "status": r.status,
            "note": r.note, "by": r.requested_by,
        }
        for r in rows
    ]


class DecideIn(BaseModel):
    approve: bool = True
    note: str = Field(default="", max_length=200)


@router.post("/api/recharge-requests/{request_id}/decide")
async def decide_recharge_request(
    request_id: str, body: DecideIn, request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve = credits turant chadh jaate hain (ledger + audit ke saath).
    Reject = kuch nahi badalta, bas request band ho jaati hai."""
    import uuid as _uuid

    from app.models import CreditLedger, RechargeRequest

    try:
        rid = _uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid request id")
    r = await db.get(RechargeRequest, rid)
    if r is None:
        raise HTTPException(status_code=404, detail="Request nahi mila")
    if r.status != "pending":
        raise HTTPException(status_code=409, detail=f"Ye request pehle hi {r.status} hai")
    who = getattr(request.state, "vendor_label", "control")
    t = await db.get(Tenant, r.tenant_id)
    balance = None
    if body.approve:
        col = "ai_credits" if r.kind == "ai" else "wa_credits"
        balance = getattr(t, col) + r.units
        setattr(t, col, balance)
        db.add(
            CreditLedger(
                tenant_id=t.id, kind=r.kind, amount=r.units, balance_after=balance,
                reason=f"{r.pack} · ₹{r.amount_inr}" + (f" · {body.note.strip()}" if body.note.strip() else ""),
                created_by=who,
            )
        )
    r.status = "approved" if body.approve else "rejected"
    r.decided_by = who
    r.decided_at = datetime.now(timezone.utc)
    if body.note.strip():
        r.note = (r.note or "") + f" | {body.note.strip()}"
    await db.commit()
    from app.services import audit

    await audit.record(
        actor_role="admin", actor=who,
        action="recharge_approved" if body.approve else "recharge_rejected",
        tenant_id=t.id,
        args={"tenant": t.slug, "pack": r.pack, "units": r.units,
              "amount_inr": r.amount_inr, "balance": balance,
              "ip": request.client.host if request.client else None},
    )
    log.info("recharge_decided", tenant=t.slug, pack=r.pack, approve=body.approve)
    return {"ok": True, "status": r.status, "balance": balance}
