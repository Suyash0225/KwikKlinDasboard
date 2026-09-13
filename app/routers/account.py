"""Bechne ka poora rasta: pricing -> signup -> payment -> login.

    GET  /api/plans              pricing page ka data (public)
    POST /api/signup             shop banao (trial), owner user + password
    POST /api/checkout           Razorpay order banao (paid karna ho to)
    POST /api/checkout/confirm   browser se aaya success — signature verify
    POST /webhooks/razorpay      asli sach: paisa aaya -> account ACTIVE
    POST /api/login              email + password -> session cookie
    POST /api/logout
    GET  /api/me                 kaun logged in hai, kaunsa plan, kya limits
    POST /api/me/password        password badlo (sab sessions band)

Owner (Suyash) ke apne endpoints `/api/admin/tenants*` par hain — usse wo
50-60 clients ek jagah se dekh aur sambhal sakta hai.
"""

import json
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    ROLE_OWNER,
    ROLES,
    TENANT_TRIAL,
    RechargeRequest,
    Tenant,
    User,
)
from app.models.tenant import GRACE_DAYS, TENANT_LOCKED, TENANT_PAST_DUE, WRITABLE_STATUSES
from app.services import auth, billing, google_auth, plans, tenant_context
from app.utils.phone import normalize_phone

log = structlog.get_logger()

router = APIRouter(tags=["account"])


# --------------------------------------------------------------------------
# public: plans
# --------------------------------------------------------------------------


@router.get("/api/plans")
async def list_plans() -> dict:
    return {
        "plans": plans.public_catalog(),
        "setup_fee_inr": plans.SETUP_FEE_INR,
        "trial_days": plans.TRIAL_DAYS,
        "gst_percent": billing.GST_PERCENT,
        "billing_live": billing.enabled(),
        "google_login": google_auth.enabled(),
    }


# --------------------------------------------------------------------------
# signup
# --------------------------------------------------------------------------


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (s or "shop")[:32]


class SignupIn(BaseModel):
    shop_name: str = Field(min_length=2, max_length=120)
    owner_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=6, max_length=20)
    email: str = Field(min_length=5, max_length=160)
    city: str | None = Field(default=None, max_length=80)
    plan: str = plans.DEFAULT_PLAN
    password: str = Field(min_length=8, max_length=128)


async def _create_shop(
    db: AsyncSession,
    *,
    shop_name: str,
    owner_name: str,
    phone: str,
    email: str,
    city: str | None,
    plan: str,
    password_hash: str,
    auth_provider: str = "password",
    google_sub: str = "",
) -> tuple[Tenant, User]:
    """Ek nayi dukaan + uska owner. Password wala aur Google wala, dono rasta
    yahi se guzarta hai — warna do jagah do niyam ban jaate."""
    email = (email or "").strip().lower()
    if "@" not in email or " " in email:
        raise HTTPException(status_code=400, detail="That email doesn't look right")
    try:
        phone_n = normalize_phone(phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # NOTE: sirf TENANTS mein dekha jaata hai. Is dukaan ka koi customer khud
    # bhi laundry chala sakta hai — customers table se yahan koi lena-dena nahi.
    dupe = (
        await db.execute(select(Tenant).where(Tenant.owner_phone == phone_n))
    ).scalar_one_or_none()
    if dupe is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This number already has an account for '{dupe.shop_name}'"
                + (f" ({dupe.owner_email})" if dupe.owner_email else "")
                + ". Please Login — or tell us if you forgot the password."
            ),
        )

    base = _slugify(shop_name)
    slug, n = base, 1
    while (
        await db.execute(select(Tenant.id).where(Tenant.slug == slug))
    ).scalar_one_or_none() is not None:
        n += 1
        slug = f"{base}-{n}"[:40]

    # Signup kisi dukaan ke ANDAR nahi hota — ye nayi dukaan BANATA hai.
    # Wajah aur tareeka tenant_context.system_context mein likha hai.
    async with tenant_context.system_context(db):
        return await _write_shop(
            db, slug=slug, shop_name=shop_name, owner_name=owner_name,
            phone_n=phone_n, email=email, city=city, plan=plan,
            password_hash=password_hash, auth_provider=auth_provider,
            google_sub=google_sub,
        )


async def _write_shop(
    db, *, slug, shop_name, owner_name, phone_n, email, city, plan,
    password_hash, auth_provider, google_sub,
):
    """Tenant + uska pehla owner. Hamesha system context mein — dekho upar."""
    tenant = Tenant(
        slug=slug,
        shop_name=shop_name.strip(),
        owner_name=owner_name.strip(),
        owner_phone=phone_n,
        owner_email=email,
        city=(city or "").strip() or None,
        plan=plans.get(plan).code,
        status=TENANT_TRIAL,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=plans.TRIAL_DAYS),
    )
    db.add(tenant)
    await db.flush()
    user = User(
        tenant_id=tenant.id,
        name=owner_name.strip(),
        email=email,
        phone=phone_n,
        password_hash=password_hash,
        role=ROLE_OWNER,
        auth_provider=auth_provider,
        google_sub=google_sub or None,
    )
    db.add(user)
    await db.commit()
    log.info("tenant_signed_up", slug=slug, plan=tenant.plan, via=auth_provider)
    return tenant, user


@router.post("/api/signup", status_code=201)
async def signup(body: SignupIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Naya shop (email + password). Trial mein baithta hai — paisa dene par
    ACTIVE hota hai.

    Trial pehle isliye ki client kuch dekhe bina paisa na de, aur checkout
    fail hone par uska data bacha rahe.
    """
    try:
        pw_hash = auth.hash_password(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    tenant, _user = await _create_shop(
        db,
        shop_name=body.shop_name, owner_name=body.owner_name, phone=body.phone,
        email=body.email, city=body.city, plan=body.plan, password_hash=pw_hash,
    )
    return {
        "tenant": {"slug": tenant.slug, "shop_name": tenant.shop_name, "plan": tenant.plan},
        "trial_ends_at": tenant.trial_ends_at.isoformat(),
        "next": "login",
    }


# --------------------------------------------------------------------------
# payment
# --------------------------------------------------------------------------


class CheckoutIn(BaseModel):
    plan: str = plans.DEFAULT_PLAN
    annual: bool = False


@router.post("/api/checkout")
async def checkout(
    body: CheckoutIn,
    p: auth.Principal = Depends(auth.require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """RECURRING Razorpay subscription banao — checkout isi par khulta hai.

    Har cycle Razorpay khud charge karta hai; activation hamesha webhook
    se hoti hai (subscription.activated / subscription.charged)."""
    if p.tenant is None:
        raise HTTPException(status_code=400, detail="Tenant not found")
    try:
        out = await billing.create_subscription(db, p.tenant, body.plan, annual=body.annual)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return out


class ConfirmIn(BaseModel):
    razorpay_payment_id: str
    razorpay_signature: str
    # order-mode (legacy) YA subscription-mode — jo aaya ho
    razorpay_order_id: str = ""
    razorpay_subscription_id: str = ""


@router.post("/api/checkout/confirm")
async def checkout_confirm(
    body: ConfirmIn,
    p: auth.Principal = Depends(auth.require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Browser ka success callback.

    Ye sirf UI ko turant sach dikhane ke liye hai. **Account ko ACTIVE karne
    ka asli faisla webhook karta hai** — browser jhooth bol sakta hai,
    Razorpay ka signed webhook nahi.
    """
    if body.razorpay_subscription_id:
        ok = billing.verify_subscription_checkout_signature(
            body.razorpay_payment_id, body.razorpay_subscription_id, body.razorpay_signature
        )
    else:
        ok = billing.verify_checkout_signature(
            body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
        )
    if not ok:
        log.warning("rzp_bad_checkout_signature", tenant=p.tenant.slug if p.tenant else "?")
        raise HTTPException(status_code=400, detail="Payment could not be verified")
    return {"verified": True, "status": p.tenant.status if p.tenant else None}


@router.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Paisa aaya — yahi wo jagah hai jahan account chalu hota hai."""
    raw = await request.body()
    if not billing.verify_webhook(raw, x_razorpay_signature):
        log.warning("rzp_webhook_bad_signature")
        raise HTTPException(status_code=403, detail="invalid signature")
    import json

    try:
        event = json.loads(raw.decode())
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")
    result = await billing.handle_event(db, event)
    return {"ok": True, "result": result}


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------


class LoginIn(BaseModel):
    email: str
    password: str


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.ENVIRONMENT == "production",
        path="/",
    )




def _dashboard_url(tenant: Tenant | None, is_home: bool) -> str:
    """Login/invite ke baad kahan bhejein — self-serve gate ke baad HAR
    chalu tenant apne dashboard par; sirf band accounts welcome (renew CTA)."""
    if tenant is not None and tenant.status in ("locked", "suspended", "cancelled"):
        return "/welcome"
    return "/admin"

@router.post("/api/login")
async def login(
    body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> dict:
    ip = request.client.host if request.client else "?"
    if auth.throttled(ip):
        raise HTTPException(
            status_code=429, detail="Too many wrong attempts — try again in 10 minutes"
        )
    email = (body.email or "").strip().lower()
    # Login bhi SYSTEM context ka kaam hai: jab tak user nahi mila, ye pata
    # hi nahi ki kis dukaan ka hai. Bina session ke request ka context HOME
    # hota hai, isliye users par RLS lagte hi doosri dukaan ka owner apne hi
    # account se login nahi kar paata — 401, bina kisi wajah ke.
    async with tenant_context.system_context(db):
        return await _login_inner(body, request, response, db, email, ip)


async def _login_inner(body, request, response, db, email, ip) -> dict:
    # Email sirf PER-TENANT unique hai — ek hi email do shops ka ho sakta
    # hai. Password hi batata hai kaun sa account: har candidate ke against
    # verify karo, jo match kare wahi user. (Pehle .first() tha — kaun sa
    # account milega ye row-order ki lottery thi.)
    candidates = (
        await db.execute(select(User).where(User.email == email))
    ).scalars().all()
    user = next(
        (
            u
            for u in candidates
            if u.is_active and auth.verify_password(body.password, u.password_hash)
        ),
        None,
    )
    # Galat email aur galat password ka jawab EK jaisa — warna attacker ko
    # pata chal jaata hai ki kaun sa email registered hai.
    if user is None:
        auth.note_failure(ip)
        log.warning("login_failed", email=email[:60], ip=ip)
        from app.services import audit

        await audit.record(
            actor_role="user", actor=email[:60], action="login_failed",
            args={"ip": ip}, ok=False,
        )
        raise HTTPException(status_code=401, detail="Email or password is incorrect")

    auth.clear_failures(ip)
    token = await auth.start_session(
        db, user, ip=ip, user_agent=request.headers.get("user-agent", "")
    )
    _set_cookie(response, token)
    tenant = await db.get(Tenant, user.tenant_id) if user.tenant_id else None
    is_home = await auth.is_home_user(db, user)
    from app.services import audit

    await audit.record(
        actor_role="user", actor=user.email, action="login",
        args={"ip": ip, "role": user.role,
              "tenant": tenant.slug if tenant else None},
    )
    return {
        "user": {"name": user.name, "email": user.email, "role": user.role},
        "must_change_password": user.must_change_password,
        "tenant": _tenant_out(tenant) if tenant else None,
        "is_home": is_home,
        # Client ka browser khud faisla na kare — server batata hai kahan jaana hai
        "next_url": _dashboard_url(tenant, is_home),
    }


@router.post("/api/logout")
async def logout(
    response: Response,
    # Cookie(...) zaroori hai — bare `str = ""` ko FastAPI query param
    # samajhta tha, to browser ka logout server par session revoke hi
    # nahi karta tha (cookie delete hoti thi, token 30 din zinda rehta).
    kk_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if kk_session:
        await auth.end_session(db, kk_session)
        from app.services import audit

        await audit.record(actor_role="user", actor=None, action="logout")
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


def _tenant_out(t: Tenant) -> dict:
    plan = plans.get(t.plan)
    return {
        "slug": t.slug,
        "shop_name": t.shop_name,
        "plan": plan.code,
        "plan_name": plan.name,
        "status": t.status,
        "trial_ends_at": t.trial_ends_at.isoformat() if t.trial_ends_at else None,
        "current_period_end": (
            t.current_period_end.isoformat() if t.current_period_end else None
        ),
        "onboarding_done": t.onboarding_done,
        "limits": {
            "orders_month": plan.max_orders_month,
            "staff": plan.max_staff,
            "outlets": plan.max_outlets,
            "campaign_msgs_month": plan.max_campaign_msgs_month,
        },
    }


@router.get("/api/me")
async def me(
    p: auth.Principal = Depends(auth.current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    from app.models import Order
    from app.services import quota

    used = ai_used = wa_used = staff_used = None
    plan = plans.get(p.tenant.plan) if p.tenant else None
    if p.tenant is not None:
        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        used = (
            await db.execute(
                select(func.count()).select_from(Order).where(Order.created_at >= month_start)
            )
        ).scalar_one()
        ai_used = await quota.ai_calls_this_month(db, p.user.tenant_id)
        wa_used = await quota.wa_messages_this_month(db, p.user.tenant_id)
        staff_used = (
            await db.execute(
                select(func.count()).select_from(User).where(User.tenant_id == p.user.tenant_id)
            )
        ).scalar_one()
    is_home = await auth.is_home_user(db, p.user)
    # Kya is dukaan se WhatsApp bhej sakte hain? Tenant ke apne creds, ya
    # home dukaan ke liye .env wale (whatsapp.resolve_creds ka wahi order).
    # Sirf haan/na jaata hai — token kabhi nahi. Dashboard isi se tay karta
    # hai ki "Customer notified" sach bole ya "Share on WhatsApp" dikhaye.
    wa_connected = False
    if p.tenant is not None:
        wa_connected = bool(p.tenant.wa_token and p.tenant.wa_phone_number_id) or (
            is_home
            and bool(settings.WHATSAPP_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID)
        )
    tenant_out = _tenant_out(p.tenant) if p.tenant else None
    if tenant_out is not None:
        tenant_out["wa_connected"] = wa_connected
    return {
        "user": {"name": p.user.name, "email": p.user.email, "role": p.user.role},
        "tenant": tenant_out,
        "can_write": p.can_write,
        # Kya ye user ISI deployment ki dukaan ka hai? Sirf tabhi use /admin
        # dashboard dikhaya jaata hai — warna wo kisi aur ka data hoga.
        "is_home": is_home,
        "usage": {
            "orders_this_month": used,
            "ai_calls_this_month": ai_used,
            "wa_messages_this_month": wa_used,
            "staff": staff_used,
        },
        # Feature-gating: frontend inhi flags se tabs lock karta hai.
        "features": sorted(plan.features) if plan else [],
        "plan_limits": {
            "staff": plan.staff_limit,
            "ai_usage": plan.ai_usage_limit,
            "whatsapp_messages": plan.whatsapp_message_limit,
        } if plan else None,
        # Banner/gating ke liye — dashboard yahi padhta hai.
        "subscription": _subscription_out(p.tenant),
    }


def _subscription_out(t: Tenant | None) -> dict | None:
    """Trial/subscription ki haalat, banner-ready numbers ke saath."""
    if t is None:
        return None
    now = datetime.now(timezone.utc)
    days_left = None
    if t.status == TENANT_TRIAL and t.trial_ends_at is not None:
        days_left = max(0, -(-int((t.trial_ends_at - now).total_seconds()) // 86400))
    grace_days_left = None
    if t.status == TENANT_PAST_DUE:
        ref = billing._grace_reference(t)
        if ref is not None:
            grace_days_left = max(0, GRACE_DAYS - (now - ref).days)
    return {
        "status": t.status,
        "trial_ends_at": t.trial_ends_at.isoformat() if t.trial_ends_at else None,
        "days_left": days_left,
        "read_only": t.status not in WRITABLE_STATUSES,
        "grace_days_left": grace_days_left,
        "locked": t.status == TENANT_LOCKED,
    }


@router.get("/api/session/adopt/{token}", include_in_schema=False)
async def adopt_session(token: str, db: AsyncSession = Depends(get_db)):
    """Impersonation link: valid session token -> cookie set -> dashboard.

    Token khud hi auth hai (30-min TTL, danger-key se bana, audit-logged).
    Galat/expired -> login page."""
    # Cookie abhi set nahi hui, isliye context HOME hai — par ye token kisi
    # bhi dukaan ka ho sakta hai. Token khud auth hai; dukaan uske resolve
    # hone par hi pata chalti hai.
    async with tenant_context.system_context(db):
        user = await auth.user_for_token(db, token)
    if user is None:
        return RedirectResponse(url="/#login", status_code=303)
    resp = RedirectResponse(url="/admin", status_code=303)
    _set_cookie(resp, token)
    log.info("session_adopted", user=user.email)
    return resp


# --------------------------------------------------------------------------
# Invite accept — set-password page (plaintext password kabhi nahi banta)
# --------------------------------------------------------------------------

_INVITE_FILE = Path(__file__).resolve().parent.parent / "static" / "invite.html"


@router.get("/invite/{token}", include_in_schema=False)
async def invite_page(token: str):
    """Set-password page ka shell. Token URL mein hai; page JS usse
    /api/invite/accept par bhejta hai. Yahan koi data nahi nikalta."""
    from fastapi.responses import Response as _Resp

    return _Resp(
        content=_INVITE_FILE.read_text(encoding="utf-8"),
        media_type="text/html",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


class InviteAcceptIn(BaseModel):
    token: str = Field(min_length=10)
    password: str = Field(min_length=8, max_length=128)


@router.post("/api/invite/accept")
async def invite_accept(
    body: InviteAcceptIn, request: Request, response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Token + naya password -> account active + seedha login."""
    from app.services import invites

    # Kis dukaan ka invite hai ye TOKEN batata hai — usse pehle pata nahi.
    async with tenant_context.system_context(db):
        return await _invite_accept_inner(body, request, response, db)


async def _invite_accept_inner(body, request, response, db) -> dict:
    from app.services import invites

    try:
        user = await invites.accept_invite(db, token=body.token, password=body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ip = request.client.host if request.client else ""
    token = await auth.start_session(
        db, user, ip=ip, user_agent=request.headers.get("user-agent", "")
    )
    _set_cookie(response, token)
    is_home = await auth.is_home_user(db, user)
    return {
        "ok": True,
        "user": {"name": user.name, "email": user.email, "role": user.role},
        "next_url": _dashboard_url(await db.get(Tenant, user.tenant_id), is_home),
    }


# --------------------------------------------------------------------------
# WhatsApp connect — har tenant apna number (official Meta Cloud API)
# --------------------------------------------------------------------------


class WaConnectIn(BaseModel):
    phone_number_id: str = Field(min_length=5, max_length=30)
    token: str = Field(min_length=20)
    waba_id: str = ""


@router.post("/api/whatsapp/connect")
async def whatsapp_connect(
    body: WaConnectIn,
    p: auth.Principal = Depends(auth.require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tenant apna WhatsApp number jodta hai (Meta Cloud API creds).

    Creds Graph API par LIVE validate hote hain — galat token/number yahin
    pakda jaata hai, webhook par nahi. Token store hota hai, wapas kabhi
    nahi bheja jaata (masked). Ek number = ek tenant (unique)."""
    from app.services import whatsapp

    pnid = body.phone_number_id.strip()
    if not await whatsapp.validate_credentials(pnid, body.token.strip()):
        raise HTTPException(
            status_code=400,
            detail="Meta ne creds reject kiye — phone_number_id/token check karein "
                   "(test mode: Meta App Dashboard > WhatsApp > API Setup).",
        )
    dupe = (
        await db.execute(
            select(Tenant).where(
                Tenant.wa_phone_number_id == pnid, Tenant.id != p.user.tenant_id
            )
        )
    ).scalar_one_or_none()
    if dupe is not None:
        raise HTTPException(
            status_code=409, detail="Ye WhatsApp number kisi aur account se juda hai."
        )
    t = await db.get(Tenant, p.user.tenant_id)
    t.wa_phone_number_id = pnid
    t.wa_waba_id = body.waba_id.strip() or None
    t.wa_token = body.token.strip()
    await db.commit()
    log.info("whatsapp_connected", tenant=t.slug, phone_number_id=pnid)
    from app.services import audit

    await audit.record(
        actor_role="user", actor=p.user.email, action="whatsapp_connect",
        args={"tenant": t.slug, "phone_number_id": pnid},
    )
    return {"connected": True, "phone_number_id": pnid, "waba_id": t.wa_waba_id}


@router.get("/api/whatsapp/status")
async def whatsapp_status(
    p: auth.Principal = Depends(auth.current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Kya WhatsApp juda hai? Token KABHI wapas nahi jaata — sirf mask."""
    t = await db.get(Tenant, p.user.tenant_id)
    if t is None or not t.wa_phone_number_id:
        return {"connected": False}
    return {
        "connected": True,
        "phone_number_id": t.wa_phone_number_id,
        "waba_id": t.wa_waba_id,
        "token": "••••••••" + (t.wa_token[-4:] if t.wa_token else ""),
    }


@router.get("/api/billing/invoices")
async def list_invoices(
    p: auth.Principal = Depends(auth.current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Is tenant ki saari receipts, nayi pehle. Sirf apne tenant ki —
    query explicitly tenant-scoped hai."""
    from app.models import Invoice

    rows = (
        await db.execute(
            select(Invoice)
            .where(Invoice.tenant_id == p.user.tenant_id)
            .order_by(Invoice.created_at.desc())
            .limit(100)
        )
    ).scalars().all()
    return [
        {
            "id": str(i.id),
            "payment_id": i.rzp_payment_id,
            "plan": plans.get(i.plan).name,
            "cycle": i.cycle,
            "amount_inr": round(i.amount_paise / 100, 2),
            "currency": i.currency,
            "status": i.status,
            "period_start": i.period_start.isoformat() if i.period_start else None,
            "period_end": i.period_end.isoformat() if i.period_end else None,
            "date": i.created_at.isoformat(),
        }
        for i in rows
    ]


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/api/me/password")
async def change_password(
    body: PasswordIn,
    response: Response,
    request: Request,
    p: auth.Principal = Depends(auth.current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not auth.verify_password(body.current_password, p.user.password_hash):
        raise HTTPException(status_code=401, detail="The old password is incorrect")
    p.user.password_hash = auth.hash_password(body.new_password)
    p.user.must_change_password = False
    await db.commit()
    # har purani jagah se logout — chori hui session bachni nahi chahiye
    await auth.end_all_sessions(db, p.user.id)
    token = await auth.start_session(
        db, p.user, ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
    )
    _set_cookie(response, token)
    log.info("password_changed", user=p.user.email)
    return {"ok": True}


# --------------------------------------------------------------------------
# Google se login
# --------------------------------------------------------------------------


@router.get("/api/auth/google/start")
async def google_start(db: AsyncSession = Depends(get_db)) -> Response:
    """Consent screen par bhej do, CSRF state cookie ke saath."""
    if not google_auth.enabled():
        raise HTTPException(
            status_code=503, detail="Google login is not configured yet"
        )
    state = google_auth.new_state()
    base = await google_auth.public_base(db)
    resp = RedirectResponse(url=google_auth.start_url(state, base), status_code=302)
    resp.set_cookie(
        google_auth.STATE_COOKIE, state, max_age=600, httponly=True,
        samesite="lax", secure=settings.ENVIRONMENT == "production", path="/",
    )
    return resp


def _fail(reason: str) -> RedirectResponse:
    """Login page par wapas, ek padhne layak wajah ke saath."""
    from urllib.parse import quote

    return RedirectResponse(url=f"/?err={quote(reason[:120])}#login", status_code=303)


@router.get("/api/auth/google/callback")
async def google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Google se wapas — poora kaam system context mein.

    Yahan user ko google_sub ya email se dhoondha jaata hai, aur wo kis
    dukaan ka hai ye milne se PEHLE pata nahi. Home tenant ke context mein
    dhoondhne par doosri dukaan ka owner apne hi Google account se andar
    nahi aa paata.

    Yahan teen mein se ek hota hai:

    1. is Google account ka user pehle se hai -> seedha login
    2. usi email par password wala account hai -> dono ko jod do
    3. bilkul naya -> dukaan ki baaki detail poochne ke liye signup par bhejo
    """
    async with tenant_context.system_context(db):
        return await _google_callback_inner(request, code, state, error, db)


async def _google_callback_inner(request, code, state, error, db) -> Response:
    if error:
        return _fail("Google login cancel ho gaya")
    cookie_state = request.cookies.get(google_auth.STATE_COOKIE, "")
    if not code or not state or state != cookie_state:
        log.warning("google_state_mismatch")
        return _fail("Login link purana ya galat tha — dobara try karein")

    try:
        ident = await google_auth.exchange_code(code, await google_auth.public_base(db))
    except google_auth.GoogleAuthError as exc:
        return _fail(str(exc))

    user = None
    if ident["sub"]:
        user = (
            await db.execute(select(User).where(User.google_sub == ident["sub"]))
        ).scalars().first()
    if user is None:
        user = (
            await db.execute(select(User).where(User.email == ident["email"]))
        ).scalars().first()
        if user is not None and not user.google_sub:
            # wahi insaan, doosra rasta — account jod do
            user.google_sub = ident["sub"]
            await db.commit()
            log.info("google_linked_to_existing", email=user.email)

    if user is not None:
        if not user.is_active:
            return _fail("Ye account band hai")
        token = await auth.start_session(
            db, user, ip=request.client.host if request.client else "",
            user_agent=request.headers.get("user-agent", ""),
        )
        is_home = await auth.is_home_user(db, user)
        resp = RedirectResponse(url=_dashboard_url(await db.get(Tenant, user.tenant_id), is_home), status_code=303)
        _set_cookie(resp, token)
        resp.delete_cookie(google_auth.STATE_COOKIE, path="/")
        log.info("google_login_ok", email=user.email, is_home=is_home)
        return resp

    # naya banda: pehchaan sambhal ke rakho, baaki detail form se lo
    resp = RedirectResponse(url="/?google=1#signup", status_code=303)
    resp.set_cookie(
        google_auth.PENDING_COOKIE,
        json.dumps({"sub": ident["sub"], "email": ident["email"], "name": ident["name"]}),
        max_age=1800, httponly=True, samesite="lax",
        secure=settings.ENVIRONMENT == "production", path="/",
    )
    resp.delete_cookie(google_auth.STATE_COOKIE, path="/")
    log.info("google_new_user_needs_shop", email=ident["email"])
    return resp


@router.get("/api/auth/google/pending")
async def google_pending(request: Request) -> dict:
    """Signup form ise padh kar naam/email pehle se bhar deta hai."""
    raw = request.cookies.get(google_auth.PENDING_COOKIE, "")
    if not raw:
        return {"pending": False}
    try:
        d = json.loads(raw)
    except Exception:
        return {"pending": False}
    return {"pending": True, "email": d.get("email"), "name": d.get("name")}


class GoogleSignupIn(BaseModel):
    """Google ne email/naam de diya — sirf dukaan ki baatein poochni hain."""

    shop_name: str = Field(min_length=2, max_length=120)
    owner_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=6, max_length=20)
    city: str | None = Field(default=None, max_length=80)
    plan: str = plans.DEFAULT_PLAN


@router.post("/api/signup/google", status_code=201)
async def signup_google(
    body: GoogleSignupIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Google se aaye bande ka shop banao. Email hum Google se lete hain,
    form se NAHI — warna koi bhi kisi aur ka email likh kar account bana le."""
    # Nayi dukaan banti hai AUR uska session shuru hota hai. Dono ek
    # hi block mein: _create_shop ke baad context wapas home ho jaata
    # hai, aur tab start_session naye user par UPDATE karta hai jo RLS
    # ke tahat 0 rows match karta — StaleDataError, bina wajah bataye.
    async with tenant_context.system_context(db):
        return await _signup_google_inner(body, request, response, db)


async def _signup_google_inner(body, request, response, db) -> dict:
    raw = request.cookies.get(google_auth.PENDING_COOKIE, "")
    if not raw:
        raise HTTPException(
            status_code=400, detail="Google login expired — please try again"
        )
    try:
        pend = json.loads(raw)
        email = (pend["email"] or "").strip().lower()
        sub = str(pend.get("sub") or "")
    except Exception:
        raise HTTPException(status_code=400, detail="Could not read the Google login")
    if not email:
        raise HTTPException(status_code=400, detail="Google did not return an email")

    tenant, user = await _create_shop(
        db,
        shop_name=body.shop_name, owner_name=body.owner_name, phone=body.phone,
        email=email, city=body.city, plan=body.plan,
        # Google wale ka koi password nahi — ye nishaan verify_password() se
        # kabhi pass nahi hota, isliye password se andar aana namumkin hai.
        password_hash="google$no-password",
        auth_provider="google", google_sub=sub,
    )
    token = await auth.start_session(
        db, user, ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
    )
    _set_cookie(response, token)
    response.delete_cookie(google_auth.PENDING_COOKIE, path="/")
    is_home = await auth.is_home_user(db, user)
    return {
        "tenant": {"slug": tenant.slug, "shop_name": tenant.shop_name, "plan": tenant.plan},
        "next_url": _dashboard_url(tenant, is_home),
    }


# --------------------------------------------------------------------------
# staff users (owner/manager)
# --------------------------------------------------------------------------


class UserIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=160)
    phone: str | None = None
    role: str = "MANAGER"


@router.get("/api/users")
async def list_users(
    p: auth.Principal = Depends(auth.require_manager), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    rows = (
        (await db.execute(select(User).where(User.tenant_id == p.user.tenant_id)))
        .scalars()
        .all()
    )
    return [
        {
            "name": u.name, "email": u.email, "phone": u.phone, "role": u.role,
            "is_active": u.is_active,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        }
        for u in rows
    ]


@router.post("/api/users", status_code=201)
async def create_user(
    body: UserIn,
    p: auth.Principal = Depends(auth.require_owner_write),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if body.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of: {', '.join(ROLES)}")
    plan = plans.get(p.tenant.plan if p.tenant else "starter")
    _max_staff = plans.effective_limits(p.tenant)["max_staff"]
    count = (
        await db.execute(
            select(func.count()).select_from(User).where(User.tenant_id == p.user.tenant_id)
        )
    ).scalar_one()
    if _max_staff is not None and count >= _max_staff:
        nxt = plans.next_plan_after(plan.code)
        raise HTTPException(
            status_code=402,
            detail=f"The {plan.name} plan allows up to {_max_staff} team members."
                   + (f" {plans.get(nxt).name} par jaayein." if nxt else ""),
        )
    email = body.email.strip().lower()
    dupe = (
        await db.execute(
            select(User).where(User.tenant_id == p.user.tenant_id, User.email == email)
        )
    ).scalar_one_or_none()
    if dupe is not None:
        raise HTTPException(status_code=409, detail="This email is already registered")

    # Invite link — password na hum banate hain, na store, na bhejte.
    # Naya user link par khud password set karta hai (services/invites.py).
    from app.services import invites

    try:
        _u, invite_path = await invites.create_invite(
            db, tenant_id=p.user.tenant_id, email=email, name=body.name,
            role=body.role, invited_by=p.user.email,
            phone=(body.phone or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    log.info("user_invited", email=email, role=body.role)
    from app.services import audit

    await audit.record(
        actor_role="user", actor=p.user.email, action="user_invited",
        args={"email": email, "role": body.role},
    )
    return {"email": email, "role": body.role, "invite_path": invite_path}


# --------------------------------------------------------------------------
# Client ka billing page: plan, usage, price card, recharge request
# --------------------------------------------------------------------------


@router.get("/api/billing/summary")
async def billing_summary(
    p: auth.Principal = Depends(auth.current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Client ke billing page ka sara data ek call mein.

    Yahan sirf USI tenant ka apna hisaab jaata hai — koi vendor-level
    cost/margin kabhi nahi (public_packs() cost hata deta hai).
    """
    from app.models import Order
    from app.services import app_settings, quota

    t = p.tenant
    if t is None:
        raise HTTPException(status_code=400, detail="Tenant not found")
    plan = plans.get(t.plan)
    limits = plans.effective_limits(t)
    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    orders_used = (
        await db.execute(
            select(func.count()).select_from(Order).where(Order.created_at >= month_start)
        )
    ).scalar_one()
    ai_used = await quota.ai_calls_this_month(db, t.id)
    wa_used = await quota.wa_messages_this_month(db, t.id)
    pending = (
        await db.execute(
            select(func.count()).select_from(RechargeRequest).where(
                RechargeRequest.tenant_id == t.id, RechargeRequest.status == "pending"
            )
        )
    ).scalar_one()
    return {
        "shop": t.shop_name,
        "plan": {"code": plan.code, "name": plan.name, "price_inr": plan.price_inr,
                 "cycle": t.billing_cycle, "points": plan.sales_points},
        "subscription": _subscription_out(t),
        "usage": {
            "orders": {"used": orders_used, "limit": limits["max_orders_month"]},
            "messages": {"used": wa_used, "limit": limits["whatsapp_message_limit"],
                         "credits": t.wa_credits},
            "ai": {"used": ai_used, "limit": limits["ai_usage_limit"],
                   "credits": t.ai_credits},
        },
        "packs": plans.public_packs(),
        "upi": {
            "id": await app_settings.get(db, "vendor_upi_id"),
            "name": await app_settings.get(db, "vendor_upi_name"),
        },
        "pending_requests": pending,
        "plans": plans.public_catalog(),
    }


class RechargeRequestIn(BaseModel):
    pack: str
    note: str = Field(default="", max_length=200)


@router.post("/api/billing/recharge-request", status_code=201)
async def request_recharge(
    body: RechargeRequestIn,
    p: auth.Principal = Depends(auth.require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Client: "ye pack chahiye". Paisa UPI se aata hai, credits vendor ke
    approve karte hi chadhte hain (control panel se)."""
    pack = plans.pack(body.pack)
    if pack is None:
        raise HTTPException(status_code=400, detail="Unknown pack")
    req = RechargeRequest(
        tenant_id=p.user.tenant_id, pack=pack["id"], kind=pack["kind"],
        units=pack["units"], amount_inr=pack["price_inr"],
        note=body.note.strip() or None, requested_by=p.user.email,
    )
    db.add(req)
    await db.commit()
    from app.services import audit

    await audit.record(
        actor_role="user", actor=p.user.email, action="recharge_requested",
        tenant_id=p.user.tenant_id,
        args={"pack": pack["id"], "units": pack["units"], "amount_inr": pack["price_inr"],
              "note": body.note.strip() or None},
    )
    log.info("recharge_requested", tenant=p.tenant.slug if p.tenant else None,
             pack=pack["id"])
    return {"ok": True, "pack": pack["id"], "amount_inr": pack["price_inr"],
            "status": "pending"}


@router.get("/api/billing/requests")
async def my_recharge_requests(
    p: auth.Principal = Depends(auth.current_user), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    rows = (
        await db.execute(
            select(RechargeRequest)
            .where(RechargeRequest.tenant_id == p.user.tenant_id)
            .order_by(RechargeRequest.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return [
        {"at": r.created_at.isoformat(), "pack": r.pack, "units": r.units,
         "amount_inr": r.amount_inr, "status": r.status, "note": r.note}
        for r in rows
    ]
