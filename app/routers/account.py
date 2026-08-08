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
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    ROLE_OWNER,
    ROLES,
    TENANT_ACTIVE,
    TENANT_TRIAL,
    LoginSession,
    Tenant,
    User,
)
from app.services import auth, billing, google_auth, plans
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
    """Razorpay order — checkout isi par khulta hai."""
    if p.tenant is None:
        raise HTTPException(status_code=400, detail="Tenant not found")
    try:
        out = await billing.create_order(p.tenant, body.plan, annual=body.annual)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return out


class ConfirmIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


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
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalars().first()
    # Galat email aur galat password ka jawab EK jaisa — warna attacker ko
    # pata chal jaata hai ki kaun sa email registered hai.
    if user is None or not user.is_active or not auth.verify_password(
        body.password, user.password_hash
    ):
        auth.note_failure(ip)
        log.warning("login_failed", email=email[:60], ip=ip)
        raise HTTPException(status_code=401, detail="Email or password is incorrect")

    auth.clear_failures(ip)
    token = await auth.start_session(
        db, user, ip=ip, user_agent=request.headers.get("user-agent", "")
    )
    _set_cookie(response, token)
    tenant = await db.get(Tenant, user.tenant_id) if user.tenant_id else None
    is_home = await auth.is_home_user(db, user)
    return {
        "user": {"name": user.name, "email": user.email, "role": user.role},
        "must_change_password": user.must_change_password,
        "tenant": _tenant_out(tenant) if tenant else None,
        "is_home": is_home,
        # Client ka browser khud faisla na kare — server batata hai kahan jaana hai
        "next_url": "/admin" if is_home else "/welcome",
    }


@router.post("/api/logout")
async def logout(
    response: Response, kk_session: str = "", db: AsyncSession = Depends(get_db)
) -> dict:
    if kk_session:
        await auth.end_session(db, kk_session)
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

    used = None
    if p.tenant is not None:
        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        used = (
            await db.execute(
                select(func.count()).select_from(Order).where(Order.created_at >= month_start)
            )
        ).scalar_one()
    return {
        "user": {"name": p.user.name, "email": p.user.email, "role": p.user.role},
        "tenant": _tenant_out(p.tenant) if p.tenant else None,
        "can_write": p.can_write,
        # Kya ye user ISI deployment ki dukaan ka hai? Sirf tabhi use /admin
        # dashboard dikhaya jaata hai — warna wo kisi aur ka data hoga.
        "is_home": await auth.is_home_user(db, p.user),
        "usage": {"orders_this_month": used},
    }


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
    """Google se wapas. Yahan teen mein se ek hota hai:

    1. is Google account ka user pehle se hai -> seedha login
    2. usi email par password wala account hai -> dono ko jod do
    3. bilkul naya -> dukaan ki baaki detail poochne ke liye signup par bhejo
    """
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
        resp = RedirectResponse(url="/admin" if is_home else "/welcome", status_code=303)
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
        "next_url": "/admin" if is_home else "/welcome",
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
    count = (
        await db.execute(
            select(func.count()).select_from(User).where(User.tenant_id == p.user.tenant_id)
        )
    ).scalar_one()
    if plan.max_staff is not None and count >= plan.max_staff:
        nxt = plans.next_plan_after(plan.code)
        raise HTTPException(
            status_code=402,
            detail=f"The {plan.name} plan allows up to {plan.max_staff} team members."
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

    temp = auth.temp_password()
    db.add(
        User(
            tenant_id=p.user.tenant_id,
            name=body.name.strip(),
            email=email,
            phone=(body.phone or "").strip() or None,
            password_hash=auth.hash_password(temp),
            role=body.role,
            must_change_password=True,
        )
    )
    await db.commit()
    log.info("user_created", email=email, role=body.role)
    # temp password SIRF yahan dikhta hai — kahin store nahi hota
    return {"email": email, "temp_password": temp, "role": body.role}
