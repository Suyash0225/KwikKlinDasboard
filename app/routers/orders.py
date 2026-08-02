"""Internal order CRUD API — how the manager (and later the dashboard/CLI)
creates and moves orders until the AI agent arrives.

Auth: every endpoint requires the X-API-Key header == settings.ADMIN_API_KEY.
This API is internal — it is NOT exposed to customers or staff.
"""

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Customer, Order, OrderStatus, OrderStatusHistory
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


async def require_admin_key(x_api_key: str = Header(default="")) -> None:
    """Dependency: reject any request without the correct X-API-Key."""
    if x_api_key != settings.ADMIN_API_KEY:
        log.warning("admin_api_bad_key")
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


async def _order_out(db: AsyncSession, order: Order, include_notes: bool = False) -> OrderOut:
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
        )
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
    return await _order_out(db, order, include_notes=True)


@router.get("", dependencies=[Depends(require_admin_key)])
async def list_orders(
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(default=None, description="status NAME, e.g. IN_WASH"),
    active: bool = Query(default=False, description="only not-finished orders"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[OrderOut]:
    q = select(Order).order_by(Order.created_at.desc()).limit(limit)
    if status:
        try:
            q = q.where(Order.status == OrderStatus[status.upper()])
        except KeyError:
            raise HTTPException(status_code=400, detail=f"unknown status {status!r}")
    elif active:
        q = q.where(Order.status.in_(order_service.ACTIVE_STATUSES))
    orders = (await db.execute(q)).scalars().all()
    return [await _order_out(db, o) for o in orders]


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