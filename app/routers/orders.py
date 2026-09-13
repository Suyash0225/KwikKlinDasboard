"""Internal order CRUD API — how the manager (and later the dashboard/CLI)
creates and moves orders until the AI agent arrives.

Auth: every endpoint requires the X-API-Key header == settings.ADMIN_API_KEY.
This API is internal — it is NOT exposed to customers or staff.
"""

import hmac
import time
from collections import defaultdict, deque
from datetime import date
from decimal import Decimal

import structlog
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete, select, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    CouponRedemption,
    Customer,
    Escalation,
    OpenQuestion,
    Order,
    OrderStatus,
    OrderStatusHistory,
    Payment,
    Task,
)
from app.services import audit
from app.schemas.orders import (
    DeliveryDateIn,
    OrderCreateIn,
    OrderOut,
    PaymentIn,
    StatusHistoryOut,
    StatusUpdateIn,
)
from app.services import order_service
from app.services.order_service import (
    InvalidTransitionError,
    OrderError,
    OrderNotFoundError,
    PlanLimitError,
)

router = APIRouter(prefix="/orders", tags=["orders"])
log = structlog.get_logger()


# Brute-force throttle: per-IP sliding window of failed key attempts.
# In-memory is fine — a restart resets it, but so does it reset the attacker's
# progress, and the key itself is 40+ random chars.
_FAILED_AUTH: dict[str, deque] = defaultdict(lambda: deque(maxlen=32))
_AUTH_WINDOW_SECS = 600
_AUTH_MAX_FAILURES = 10


def _auth_throttled(ip: str) -> bool:
    now = time.monotonic()
    attempts = _FAILED_AUTH[ip]
    while attempts and now - attempts[0] > _AUTH_WINDOW_SECS:
        attempts.popleft()
    return len(attempts) >= _AUTH_MAX_FAILURES


async def require_admin_key(
    request: Request,
    x_api_key: str = Header(default=""),
    kk_session: str = Cookie(default=""),
) -> None:
    """This shop's data — nobody else's.

    Two ways in, and BOTH are scoped to this deployment's own shop:

    1. a login session whose user belongs to the HOME tenant (browser), or
    2. the X-API-Key (scripts, and the owner's own tooling).

    A session belonging to some OTHER tenant — anyone who just signed up on
    the public page — is refused with 403. That hole is how a brand-new
    signup was able to open this shop's dashboard: the page only ever
    checked the API key, and a browser that already had the owner's key
    cached sailed straight in.
    """
    ip = request.client.host if request.client else "?"
    if _auth_throttled(ip):
        log.warning("admin_api_throttled", ip=ip)
        raise HTTPException(status_code=429, detail="too many failed attempts — wait 10 minutes")

    # 1. session first — that is what a real browser user has
    #
    # SELF-SERVE GATE: HAR tenant ka user apna dashboard use karta hai.
    # Data isolation yahan bharosa nahi, guarantee hai: middleware ne ctx
    # isi session ke tenant par set kiya hai -> RLS + ORM filter har query
    # ko usi tenant tak scope karte hain (test_selfserve_gate.py proof).
    if kk_session:
        from app.database import async_session_factory
        from app.services import auth as auth_service

        async with async_session_factory() as db:
            user = await auth_service.user_for_token(db, kk_session)
            if user is not None:
                request.state.user_email = user.email
                # Role neeche require_admin_owner jaise gates padhte hain.
                request.state.admin_role = user.role
                await _enforce_tenant_billing(request, db, user.tenant_id)
                return

    # 2. API key
    if not _key_matches(x_api_key):
        if x_api_key:  # galat key = attempt; khali = bas logged-out request
            _FAILED_AUTH[ip].append(time.monotonic())
            log.warning("admin_api_bad_key", ip=ip, path=request.url.path)
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    # Key = owner's own tooling — full access (home tenant par).
    request.state.admin_role = "key"
    from app.database import async_session_factory as _asf

    async with _asf() as db:
        await _enforce_tenant_billing(request, db, None)


async def _enforce_tenant_billing(
    request: Request, db: AsyncSession, tenant_id
) -> None:
    """Trial/subscription gate — har dashboard request par, USI tenant ke
    status par jiska session hai (key path = home tenant).

    locked   -> kuch nahi chalta (reads bhi nahi); data DB mein safe hai,
                payment aate hi sab wapas. /api/me isse bahar hai taaki
                user apni haalat dekh sake.
    past_due -> READ-ONLY: GET chalta hai, POST/PUT/DELETE par 402.
    (Vendor ka /control require_vendor_key par hai — us par gate nahi.)
    """
    from app.models.tenant import TENANT_LOCKED, WRITABLE_STATUSES, Tenant
    from app.services import auth as auth_service

    if tenant_id is not None:
        home = await db.get(Tenant, tenant_id)
    else:
        home = await auth_service.home_tenant(db)
    if home is None:
        return
    if home.status == TENANT_LOCKED:
        raise HTTPException(
            status_code=402,
            detail="Account locked hai — subscription renew karein. "
                   "Aapka poora data safe hai, kuch bhi delete nahi hua.",
        )
    if request.method not in ("GET", "HEAD", "OPTIONS") and (
        home.status not in WRITABLE_STATUSES
    ):
        raise HTTPException(
            status_code=402,
            detail="Account read-only hai (trial/subscription khatam). "
                   "Renew karte hi likhna wapas chalu — data safe hai.",
        )


def require_feature(feature: str):
    """Route dependency: ye feature home tenant ke PLAN mein on hona chahiye.

    Auth pehle chalta hai (require_admin_key — cached, dobara nahi chalta).
    Feature off -> 402 with "Upgrade" detail; frontend isi se Upgrade
    prompt dikhata hai. Naya plan/feature = sirf plans.py mein entry.
    """

    async def _dep(request: Request, _: None = Depends(require_admin_key)) -> None:
        from app.database import async_session_factory as _asf
        from app.models.tenant import Tenant as _T
        from app.services import auth as auth_service
        from app.services import plans, tenant_context

        # SESSION tenant ka plan (ctx middleware ne set kiya); key path = home
        tid = tenant_context.current_tenant_id.get()
        async with _asf() as db:
            home = (await db.get(_T, tid)) if tid else await auth_service.home_tenant(db)
        code = home.plan if home else plans.DEFAULT_PLAN
        if plans.feature_on(code, feature):
            return
        need = plans.plan_with_feature(feature)
        hint = f" {plans.get(need).name} plan mein milega." if need else ""
        log.info("feature_blocked", feature=feature, plan=code, path=request.url.path)
        raise HTTPException(
            status_code=402,
            detail=f"Upgrade needed: ye feature ({feature}) aapke "
                   f"{plans.get(code).name} plan mein nahi hai.{hint}",
        )

    _dep.__name__ = f"require_feature_{feature}"
    return _dep


def _key_matches(candidate: str) -> bool:
    """Constant-time key compare; non-ASCII junk = mismatch, not a 500."""
    try:
        return hmac.compare_digest(candidate, settings.ADMIN_API_KEY)
    except TypeError:
        return False


def vendor_master_key() -> str:
    """/control ka master key. VENDOR_API_KEY set ho to wahi — ADMIN_API_KEY
    (dukaan ka key) control par NAHI chalta. Set na ho to purana raasta:
    ADMIN_API_KEY hi, taaki ek-dukaan wala deploy bina .env badle chale."""
    return settings.VENDOR_API_KEY or settings.ADMIN_API_KEY


def _vendor_key_matches(candidate: str) -> bool:
    try:
        return hmac.compare_digest(candidate, vendor_master_key())
    except TypeError:
        return False


async def require_admin_owner(
    request: Request, _: None = Depends(require_admin_key)
) -> None:
    """Owner-level dashboard endpoints: paisa, settings, staff, exports.

    Pehle require_admin_key chalta hai (home-tenant session ya API key).
    Uske upar: session wale user ka role OWNER/MANAGER hona chahiye —
    STAFF/ACCOUNTANT ko 403. API-key path hamesha full access hai
    (wo owner ki apni tooling hai).
    """
    from app.models.tenant import ROLE_MANAGER, ROLE_OWNER

    role = getattr(request.state, "admin_role", None)
    if role in ("key", ROLE_OWNER, ROLE_MANAGER):
        return
    log.warning(
        "admin_role_blocked",
        role=role, path=request.url.path,
        user=getattr(request.state, "user_email", None),
    )
    raise HTTPException(
        status_code=403, detail="Sirf owner/manager ke liye. Apne owner se kahein."
    )


_VENDOR_LEVELS = {"read": 0, "write": 1, "danger": 2}

# --- vendor session cookie ---------------------------------------------
# Panel ki master key ab browser storage mein NAHI rehti. Key sirf EK BAAR
# POST /control/api/session par jaati hai; server usse verify karke ye
# short-lived signed token httpOnly cookie mein set karta hai. XSS bhi ho
# jaye to JS token padh hi nahi sakta, aur key kahin persist nahi hoti.
VENDOR_COOKIE = "kk_vendor"
# Apna session kholna/band karna "write" nahi hai — warna read-level key
# panel mein sign-in hi nahi kar paati (sirf dekhne wale admin bhi bahar).
_VENDOR_SESSION_PATHS = ("/control/api/session", "/control/api/session/logout")
VENDOR_SESSION_HOURS = 8


def mint_vendor_token(level: str, label: str, kid: str = "env") -> str:
    """Signed, self-contained session token (HMAC over the vendor master key)."""
    import base64
    import hashlib

    exp = int(time.time()) + VENDOR_SESSION_HOURS * 3600
    payload = f"{level}|{label}|{kid}|{exp}"
    sig = hmac.new(
        vendor_master_key().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    raw = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{raw}.{sig}"


async def verify_vendor_token(token: str) -> tuple[str, str] | None:
    """(level, label) ya None. Per-admin key REVOKE hote hi cookie bhi
    mar jaati hai — isliye har request par kid DB mein check hota hai."""
    import base64
    import hashlib

    try:
        raw, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
        expected = hmac.new(
            vendor_master_key().encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        level, rest = payload.split("|", 1)
        label, kid, exp = rest.rsplit("|", 2)
        if level not in _VENDOR_LEVELS or int(exp) < time.time():
            return None
    except Exception:
        return None
    if kid != "env":
        from app.database import async_session_factory as _asf
        from app.models import AdminKey

        async with _asf() as db:
            k = await db.get(AdminKey, kid)
        if k is None or k.revoked_at is not None:
            return None
    return level, label


async def require_vendor_key(
    request: Request,
    x_api_key: str = Header(default=""),
    kk_vendor: str = Cookie(default=""),
) -> None:
    """Control panel (vendor ka apna) — client-user sessions yahan kabhi nahi.

    Do tarah ki keys chalti hain:
    1. Legacy .env ADMIN_API_KEY — full (danger) access, hamesha.
    2. Per-admin keys (admin_keys table, hash-only) — level ke saath:
       read (sirf GET) < write (mutations) < danger (delete/reset/keys).
       Rotation = nayi banao, purani revoke.

    Key do rasto se aa sakti hai — dono ka level/label logic EK hi hai:
    - `X-API-Key` header (scripts/curl, jaisa tha waisa hi), ya
    - `kk_vendor` httpOnly cookie (browser panel) jo POST
      /control/api/session se banti hai. Cookie sirf storage-location fix
      hai; permission model bilkul nahi badla.

    Method-level enforcement yahin: read key se koi mutation nahi.
    Danger-only routes par upar se Depends(require_vendor_danger) lagta hai.
    """
    ip = request.client.host if request.client else "?"
    if _auth_throttled(ip):
        log.warning("vendor_api_throttled", ip=ip)
        raise HTTPException(status_code=429, detail="too many failed attempts — wait 10 minutes")

    # 1. browser panel ka cookie session (key browser mein kahin nahi hoti)
    if not x_api_key and kk_vendor:
        got = await verify_vendor_token(kk_vendor)
        if got is not None:
            request.state.admin_role = "key"
            request.state.vendor_level, request.state.vendor_label = got
            request.state.vendor_via = "cookie"
            if _is_vendor_write(request) and _VENDOR_LEVELS[got[0]] < 1:
                raise HTTPException(
                    status_code=403,
                    detail=f"'{got[1]}' read-only key hai — write nahi kar sakti",
                )
            return
        log.info("vendor_cookie_invalid", ip=ip, path=request.url.path)
        raise HTTPException(status_code=401, detail="session expired — sign in again")

    level: str | None = None
    label = "env-key"
    kid = "env"
    if _vendor_key_matches(x_api_key):
        level = "danger"  # vendor master key (VENDOR_API_KEY, ya legacy ADMIN_API_KEY)
    elif x_api_key:
        import hashlib as _hl
        from datetime import datetime as _dt, timezone as _tz

        from app.database import async_session_factory as _asf
        from app.models import AdminKey

        h = _hl.sha256(x_api_key.encode()).hexdigest()
        async with _asf() as db:
            k = (
                await db.execute(
                    select(AdminKey).where(
                        AdminKey.key_hash == h, AdminKey.revoked_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if k is not None:
                k.last_used_at = _dt.now(_tz.utc)
                await db.commit()
                level, label, kid = k.level, k.label, str(k.id)

    if level is None:
        # Throttle SIRF galat credential par. "Koi credential hi nahi" (panel
        # ka pehla load, logged-out tab) attack nahi hai — use ginne se panel
        # aur dashboard dono 429 mein chale jaate the (dono ka counter ek hai).
        if x_api_key:
            _FAILED_AUTH[ip].append(time.monotonic())
            log.warning("vendor_api_bad_key", ip=ip, path=request.url.path)
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    request.state.admin_role = "key"
    request.state.vendor_level = level
    request.state.vendor_label = label
    request.state.vendor_kid = kid
    request.state.vendor_via = "header"
    if _is_vendor_write(request) and _VENDOR_LEVELS[level] < 1:
        raise HTTPException(
            status_code=403, detail=f"'{label}' read-only key hai — write nahi kar sakti"
        )


def _is_vendor_write(request: Request) -> bool:
    """Level check ke liye: kya ye request sach mein kuch badal rahi hai?"""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return False
    return request.url.path not in _VENDOR_SESSION_PATHS


async def require_vendor_danger(
    request: Request, _: None = Depends(require_vendor_key)
) -> None:
    """Delete / password-reset / key-management — sirf danger-level keys."""
    if _VENDOR_LEVELS.get(getattr(request.state, "vendor_level", "read"), 0) < 2:
        raise HTTPException(
            status_code=403,
            detail=f"'{getattr(request.state, 'vendor_label', '?')}' key ko is "
                   "action ki permission nahi (danger level chahiye)",
        )


async def _order_out(
    db: AsyncSession,
    order: Order,
    include_notes: bool = False,
    customer: Customer | None = None,
) -> OrderOut:
    if customer is None:
        customer = await db.get(Customer, order.customer_id)
    return OrderOut(
        order_number=order.order_number,
        status=order.status.name,
        customer_phone=customer.phone if customer else "?",
        customer_name=customer.name if customer else None,
        items=order.items,
        total_amount=order.total_amount,
        discount_amount=order.discount_amount,
        gst_amount=order.gst_amount,
        amount_paid=order.amount_paid,
        payment_status=order.payment_status.name,
        expected_delivery=order.expected_delivery,
        pickup_date=order.pickup_date,
        created_at=order.created_at,
        notes=order.notes if include_notes else None,
    )


@router.post("", dependencies=[Depends(require_admin_key), Depends(require_feature("billing"))], status_code=201)
async def create_order(body: OrderCreateIn, db: AsyncSession = Depends(get_db)) -> OrderOut:
    try:
        order = await order_service.create_order(
            db,
            customer_phone=body.customer_phone,
            customer_name=body.customer_name,
            items=[i.model_dump(exclude_none=True) for i in body.items],
            total_amount=body.total_amount,
            discount_amount=body.discount_amount,
            gst_amount=body.gst_amount,
            pickup_date=body.pickup_date,
            # pickup_date bhara hai matlab kapde lene jaane hain — ab ye
            # sach mein order ko delivery wale ki list mein daalta hai.
            # Pehle ye field sirf DB mein padi rehti thi.
            needs_pickup=body.pickup_date is not None,
            expected_delivery=body.expected_delivery,
            notes=body.notes,
            created_by="manager",
            advance_hint=body.advance_amount,
        )
        # Coupon: validate against the order total, redeem, adjust amounts.
        if body.coupon_code:
            from app.services.marketing_agent import redeem_coupon, validate_coupon

            coupon, discount, err = await validate_coupon(
                db, body.coupon_code, order.customer_id, order.total_amount or 0
            )
            if err:
                raise HTTPException(status_code=400, detail=f"coupon: {err}")
            order.total_amount = (order.total_amount or 0) - discount
            order.discount_amount = (order.discount_amount or 0) + discount
            order.recalculate_payment_status()
            await db.commit()
            await redeem_coupon(db, coupon, order, discount)
        # Advance taken at the counter -> record as a real payment.
        if body.advance_amount and body.advance_amount > 0:
            from app.models import PaymentMethod as PM

            await order_service.record_payment(
                db, order,
                amount=body.advance_amount,
                method=body.advance_method or PM.CASH,
            )
    except PlanLimitError as exc:
        # Limit khatam = 402, taaki dashboard Upgrade prompt dikhaye
        raise HTTPException(status_code=402, detail=str(exc))
    except (OrderError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # instant work order to staff — UI-created bills behave like chat bills
    from app.services.work_orders import send_work_order

    await send_work_order(db, order, headline="Naya order aaya")
    return await _order_out(db, order, include_notes=True)


@router.get("", dependencies=[Depends(require_admin_key)])
async def list_orders(
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(default=None, description="status NAME, e.g. IN_WASH"),
    active: bool = Query(default=False, description="only not-finished orders"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[OrderOut]:
    # one JOIN instead of a customer lookup per order (N+1 killed the p95
    # under load testing)
    q = (
        select(Order, Customer)
        .join(Customer, Order.customer_id == Customer.id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    if status:
        try:
            q = q.where(Order.status == OrderStatus[status.upper()])
        except KeyError:
            raise HTTPException(status_code=400, detail=f"unknown status {status!r}")
    elif active:
        q = q.where(Order.status.in_(order_service.ACTIVE_STATUSES))
    rows = (await db.execute(q)).all()
    return [await _order_out(db, o, customer=c) for o, c in rows]


@router.get("/{order_number}", dependencies=[Depends(require_admin_key)])
async def get_order(order_number: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        order = await order_service.get_order(db, order_number)
    except OrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    history = (
        await db.execute(
            select(OrderStatusHistory)
            .where(OrderStatusHistory.order_id == order.id)
            .order_by(OrderStatusHistory.changed_at)
        )
    ).scalars().all()
    out = await _order_out(db, order, include_notes=True)
    return {
        "order": out.model_dump(),
        "history": [
            StatusHistoryOut(
                old_status=h.old_status.name if h.old_status else None,
                new_status=h.new_status.name,
                changed_by=h.changed_by,
                changed_at=h.changed_at,
            ).model_dump()
            for h in history
        ],
    }


class OrderEditIn(BaseModel):
    """Fields an owner may correct on an existing bill.

    Money RECEIVED is not here on purpose — amount_paid is derived from the
    payments ledger, so a typo in a payment is fixed by the payment, not by
    overwriting the total.
    """

    items: list | None = None
    total_amount: Decimal | None = Field(default=None, ge=0)
    discount_amount: Decimal | None = Field(default=None, ge=0)
    gst_amount: Decimal | None = Field(default=None, ge=0)
    expected_delivery: date | None = None
    priority: str | None = Field(default=None, pattern="^(normal|urgent)$")
    notes: str | None = None
    edited_by: str = "dashboard"


@router.put("/{order_number}", dependencies=[Depends(require_admin_owner), Depends(require_feature("billing"))])
async def edit_order(
    order_number: str, body: OrderEditIn, db: AsyncSession = Depends(get_db)
) -> OrderOut:
    """Correct a bill (wrong items, wrong amount, wrong date)."""
    try:
        order = await order_service.get_order(db, order_number)
    except OrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    before = {
        "items": order.items, "total_amount": str(order.total_amount),
        "expected_delivery": str(order.expected_delivery),
    }
    if body.items is not None:
        order.items = body.items
    if body.total_amount is not None:
        order.total_amount = body.total_amount
    if body.discount_amount is not None:
        order.discount_amount = body.discount_amount
    if body.gst_amount is not None:
        order.gst_amount = body.gst_amount
    if body.expected_delivery is not None:
        order.expected_delivery = body.expected_delivery
    if body.priority is not None:
        order.priority = body.priority
    if body.notes is not None:
        order.notes = body.notes or None
    # the total may now sit above/below what was already paid
    order.recalculate_payment_status()
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        log.exception("order_edit_failed", order_number=order_number)
        raise HTTPException(status_code=400, detail="could not save the changes")

    await audit.record(
        actor_role="admin", actor=body.edited_by, action="order_edited",
        args={"order": order_number, "before": before},
        result=f"total ₹{order.total_amount} status {order.payment_status.name}",
    )
    log.info("order_edited", order_number=order_number, by=body.edited_by)
    return await _order_out(db, order, include_notes=True)


@router.delete("/{order_number}", dependencies=[Depends(require_admin_owner)])
async def delete_order(
    order_number: str,
    db: AsyncSession = Depends(get_db),
    deleted_by: str = Query(default="dashboard"),
) -> dict:
    """Delete a bill entirely — for one entered by mistake.

    Everything hanging off it goes too (payments, status history, coupon
    redemptions), otherwise the DB would keep orphan money rows that still
    show up in reports.
    """
    try:
        order = await order_service.get_order(db, order_number)
    except OrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    oid = order.id
    paid = order.amount_paid
    await db.execute(sa_delete(Payment).where(Payment.order_id == oid))
    await db.execute(sa_delete(OrderStatusHistory).where(OrderStatusHistory.order_id == oid))
    await db.execute(sa_delete(CouponRedemption).where(CouponRedemption.order_id == oid))
    await db.execute(sa_delete(Escalation).where(Escalation.order_id == oid))
    await db.execute(
        sa_update(OpenQuestion).where(OpenQuestion.order_id == oid).values(order_id=None)
    )
    # Tasks bhi order par latakte hain (pickup/work orders). Inhe delete
    # nahi karte — kaam ka record hai — bas link tod dete hain, warna FK
    # delete ko rok deta tha aur owner ko "delete nahi ho raha" dikhta tha.
    await db.execute(sa_update(Task).where(Task.order_id == oid).values(order_id=None))
    await db.delete(order)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        log.exception("order_delete_failed", order_number=order_number)
        raise HTTPException(
            status_code=409,
            detail="Is bill se juda purana record hai — delete nahi ho paya.",
        )

    await audit.record(
        actor_role="admin", actor=deleted_by, action="order_deleted",
        args={"order": order_number, "amount_paid": str(paid)},
        result="deleted with payments and history",
    )
    log.info("order_deleted", order_number=order_number, by=deleted_by)
    return {"ok": True, "deleted": order_number}


@router.post("/{order_number}/status", dependencies=[Depends(require_admin_key), Depends(require_feature("billing"))])
async def update_status(
    order_number: str, body: StatusUpdateIn, db: AsyncSession = Depends(get_db)
) -> OrderOut:
    try:
        order = await order_service.get_order(db, order_number)
        new_status = OrderStatus[body.status.upper()]
    except OrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown status {body.status!r}")
    try:
        await order_service.update_status(db, order, new_status, changed_by=body.changed_by)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return await _order_out(db, order)


@router.post("/{order_number}/payment", dependencies=[Depends(require_admin_key), Depends(require_feature("billing"))])
async def record_payment(
    order_number: str, body: PaymentIn, db: AsyncSession = Depends(get_db)
) -> OrderOut:
    try:
        order = await order_service.get_order(db, order_number)
        await order_service.record_payment(db, order, amount=body.amount, method=body.method)
    except OrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PlanLimitError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await _order_out(db, order)


@router.post("/{order_number}/delivery-date", dependencies=[Depends(require_admin_key)])
async def set_delivery_date(
    order_number: str, body: DeliveryDateIn, db: AsyncSession = Depends(get_db)
) -> OrderOut:
    try:
        order = await order_service.get_order(db, order_number)
    except OrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await order_service.set_expected_delivery(
        db,
        order,
        body.expected_delivery,
        changed_by=body.changed_by,
        internal_reason=body.internal_reason,
    )
    return await _order_out(db, order)