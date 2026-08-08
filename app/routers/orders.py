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
)
from app.utils.phone import normalize_phone

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
    if kk_session:
        from app.database import async_session_factory
        from app.services import auth as auth_service

        async with async_session_factory() as db:
            user = await auth_service.user_for_token(db, kk_session)
            if user is not None:
                if await auth_service.is_home_user(db, user):
                    request.state.user_email = user.email
                    return
                log.warning(
                    "cross_tenant_dashboard_blocked",
                    user=user.email, path=request.url.path,
                )
                raise HTTPException(
                    status_code=403,
                    detail="Ye dashboard aapke account ka nahi hai.",
                )

    # 2. API key
    if not hmac.compare_digest(x_api_key, settings.ADMIN_API_KEY):
        _FAILED_AUTH[ip].append(time.monotonic())
        log.warning("admin_api_bad_key", ip=ip, path=request.url.path)
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


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


@router.post("", dependencies=[Depends(require_admin_key)], status_code=201)
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


@router.put("/{order_number}", dependencies=[Depends(require_admin_key)])
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


@router.delete("/{order_number}", dependencies=[Depends(require_admin_key)])
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


@router.post("/{order_number}/status", dependencies=[Depends(require_admin_key)])
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


@router.post("/{order_number}/payment", dependencies=[Depends(require_admin_key)])
async def record_payment(
    order_number: str, body: PaymentIn, db: AsyncSession = Depends(get_db)
) -> OrderOut:
    try:
        order = await order_service.get_order(db, order_number)
        await order_service.record_payment(db, order, amount=body.amount, method=body.method)
    except OrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
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