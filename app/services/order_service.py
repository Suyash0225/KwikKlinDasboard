"""Order business logic: creation, status lifecycle, payments, dates.

This module is deterministic Python — in Phase 4 the AI agent will CALL these
functions as tools, never bypass them. Rules enforced here:

STATE MACHINE (owner-approved 2026-08-01):
    RECEIVED -> IN_WASH -> IN_DRY -> IN_IRON -> READY -> OUT_FOR_DELIVERY -> DELIVERED
    - Forward moves only; skipping stages is allowed ("wash only" orders
      skip IN_IRON). Backward moves are refused.
    - CANCELLED: reachable from any non-terminal state. Terminal.
    - DELIVERED: terminal.
    - ON_HOLD: reachable from any non-terminal state; resumable to any
      lifecycle stage.

ORDER NUMBERS: KK-YYYYMMDD-NN, daily sequence starting 01. Generation holds a
Postgres advisory lock so two concurrent creates can never mint the same
number.

DATES: expected_delivery is written to the DB *before* any message quotes it
(ground rule #4). Internal reasons go to orders.notes only (rule #3).
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Customer,
    Order,
    OrderStatus,
    OrderStatusHistory,
    PaymentMethod,
)
from app.services.messages import get_message
from app.services.whatsapp import SendError, WindowClosedError, send_message
from app.utils.phone import normalize_phone

log = structlog.get_logger()

ORDER_NUMBER_PREFIX = "KK"
# Arbitrary constant identifying "order number generation" for the advisory
# lock. Any two transactions using the same key serialize on it.
_ORDER_NUMBER_LOCK_KEY = 834712

_SEQUENCE = [
    OrderStatus.RECEIVED,
    OrderStatus.IN_WASH,
    OrderStatus.IN_DRY,
    OrderStatus.IN_IRON,
    OrderStatus.READY,
    OrderStatus.OUT_FOR_DELIVERY,
    OrderStatus.DELIVERED,
]
_TERMINAL = {OrderStatus.DELIVERED, OrderStatus.CANCELLED}
ACTIVE_STATUSES = [s for s in _SEQUENCE if s is not OrderStatus.DELIVERED] + [
    OrderStatus.ON_HOLD
]


class OrderError(Exception):
    """Base class for order business-rule violations."""


class InvalidTransitionError(OrderError):
    """Status change not allowed by the state machine."""


class OrderNotFoundError(OrderError):
    """No order with that number."""


def can_transition(old: OrderStatus, new: OrderStatus) -> bool:
    """The one and only transition rule. See module docstring."""
    if old == new:
        return False
    if old in _TERMINAL:
        return False
    if new in (OrderStatus.CANCELLED, OrderStatus.ON_HOLD):
        return True
    if old is OrderStatus.ON_HOLD:
        return new in _SEQUENCE
    # both are lifecycle stages: forward only (skips allowed)
    return _SEQUENCE.index(new) > _SEQUENCE.index(old)


async def create_order(
    db: AsyncSession,
    *,
    customer_phone: str,
    items: list[dict],
    customer_name: str | None = None,
    total_amount: Decimal | None = None,
    discount_amount: Decimal | None = None,
    gst_amount: Decimal | None = None,
    pickup_date: date | None = None,
    expected_delivery: date | None = None,
    notes: str | None = None,
    created_by: str = "system",
) -> Order:
    """Create an order (upserting the customer) and log RECEIVED in history.

    Commits. Returns the fresh Order.
    """
    phone = normalize_phone(customer_phone)
    if not isinstance(items, list) or not items:
        raise OrderError("items must be a non-empty list")
    for item in items:
        if not isinstance(item, dict) or "type" not in item or "qty" not in item:
            raise OrderError(f"each item needs at least type and qty: {item!r}")

    # Concurrency-safe upsert: two simultaneous creates for the same new
    # customer must not race — ON CONFLICT makes insert-or-skip atomic.
    await db.execute(
        pg_insert(Customer)
        .values(phone=phone, name=customer_name)
        .on_conflict_do_nothing(index_elements=["phone"])
    )
    customer = (
        await db.execute(select(Customer).where(Customer.phone == phone))
    ).scalar_one()
    if customer_name and not customer.name:
        customer.name = customer_name

    order_number = await _next_order_number(db)

    order = Order(
        order_number=order_number,
        customer_id=customer.id,
        status=OrderStatus.RECEIVED,
        items=items,
        total_amount=total_amount,
        discount_amount=discount_amount,
        gst_amount=gst_amount,
        pickup_date=pickup_date,
        expected_delivery=expected_delivery,
        notes=notes,
    )
    db.add(order)
    await db.flush()
    db.add(
        OrderStatusHistory(
            order_id=order.id,
            old_status=None,
            new_status=OrderStatus.RECEIVED,
            changed_by=created_by,
        )
    )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        log.exception("order_create_failed", phone=phone)
        raise
    log.info(
        "order_created",
        order_number=order_number,
        customer_phone=phone,
        items=len(items),
        created_by=created_by,
    )

    items_count = str(sum(int(i.get("qty", 1)) for i in items))
    if expected_delivery:
        await _notify_customer(
            db, order,
            message_key="order_confirmed_with_date",
            template_name="kk_order_confirmed",
            template_params=[order_number, items_count],
            items_count=items_count,
            date=_fmt_date(expected_delivery),
        )
    else:
        await _notify_customer(
            db, order,
            message_key="order_confirmed",
            template_name="kk_order_confirmed",
            template_params=[order_number, items_count],
            items_count=items_count,
        )
    return order


async def update_status(
    db: AsyncSession,
    order: Order,
    new_status: OrderStatus,
    *,
    changed_by: str,
) -> Order:
    """Move an order through the state machine. Commits.

    Raises InvalidTransitionError on a move the machine forbids.
    """
    old_status = order.status
    if not can_transition(old_status, new_status):
        log.warning(
            "invalid_status_transition",
            order_number=order.order_number,
            old=old_status.name,
            new=new_status.name,
            changed_by=changed_by,
        )
        raise InvalidTransitionError(
            f"{order.order_number}: {old_status.name} -> {new_status.name} is not allowed"
        )

    order.status = new_status
    if new_status is OrderStatus.DELIVERED:
        order.actual_delivery = datetime.now(timezone.utc)
    db.add(
        OrderStatusHistory(
            order_id=order.id,
            old_status=old_status,
            new_status=new_status,
            changed_by=changed_by,
        )
    )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        log.exception("status_update_failed", order_number=order.order_number)
        raise
    log.info(
        "order_status_changed",
        order_number=order.order_number,
        old=old_status.name,
        new=new_status.name,
        changed_by=changed_by,
    )

    notification = _STATUS_NOTIFICATIONS.get(new_status)
    if notification:
        message_key, template_name = notification
        await _notify_customer(
            db, order,
            message_key=message_key,
            template_name=template_name,
            template_params=[order.order_number],
        )
    return order


async def get_order(db: AsyncSession, order_number: str) -> Order:
    """Fetch by order number. Raises OrderNotFoundError."""
    order = (
        await db.execute(select(Order).where(Order.order_number == order_number.upper()))
    ).scalar_one_or_none()
    if order is None:
        raise OrderNotFoundError(f"no order {order_number!r}")
    return order


async def get_active_orders_for_phone(db: AsyncSession, phone: str) -> list[Order]:
    """All not-finished orders for a customer phone (empty list if none)."""
    normalized = normalize_phone(phone)
    rows = (
        await db.execute(
            select(Order)
            .join(Customer, Customer.id == Order.customer_id)
            .where(Customer.phone == normalized, Order.status.in_(ACTIVE_STATUSES))
            .order_by(Order.created_at.desc())
        )
    ).scalars().all()
    return list(rows)


async def record_payment(
    db: AsyncSession,
    order: Order,
    *,
    amount: Decimal,
    method: PaymentMethod,
    recorded_by: str = "dashboard",
    note: str | None = None,
) -> Order:
    """Add a received payment; payment_status derives from the model rule.

    Writes an append-only Payment ledger row (source of truth for reports)
    AND updates orders.amount_paid (derived cache the rest of the app reads).
    """
    if amount <= 0:
        raise OrderError("payment amount must be positive")
    from app.models import Payment, PaymentStatus  # local import avoids cycle noise

    db.add(
        Payment(
            order_id=order.id,
            amount=amount,
            method=method,
            recorded_by=recorded_by[:80],
            note=(note or None),
        )
    )
    order.amount_paid = (order.amount_paid or Decimal("0")) + amount
    order.payment_method = method
    order.recalculate_payment_status()  # THE one place for the rule

    if order.payment_status is PaymentStatus.PAID and order.paid_at is None:
        order.paid_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        log.exception("payment_record_failed", order_number=order.order_number)
        raise
    log.info(
        "payment_recorded",
        order_number=order.order_number,
        amount=str(amount),
        method=method.name,
        payment_status=order.payment_status.name,
    )
    return order


async def set_expected_delivery(
    db: AsyncSession,
    order: Order,
    new_date: date,
    *,
    changed_by: str,
    internal_reason: str | None = None,
) -> Order:
    """Set/revise the promised date. DB write FIRST — messages quote only this.

    internal_reason lands in orders.notes (manager-only) and must NEVER be
    sent to the customer (ground rule #3).
    """
    order.expected_delivery = new_date
    if internal_reason:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        line = f"[{stamp} {changed_by}] {internal_reason}"
        order.notes = f"{order.notes}\n{line}" if order.notes else line
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        log.exception("expected_delivery_update_failed", order_number=order.order_number)
        raise
    log.info(
        "expected_delivery_set",
        order_number=order.order_number,
        new_date=str(new_date),
        changed_by=changed_by,
        has_internal_reason=bool(internal_reason),
    )

    # Date is committed above — only now may a message quote it (rule #4).
    # The internal_reason is NOT passed anywhere near this text (rule #3).
    if order.status not in _TERMINAL:
        await _notify_customer(
            db, order,
            message_key="delay_notice",
            template_name="kk_delay_notice",
            template_params=[order.order_number, _fmt_date(new_date)],
            date=_fmt_date(new_date),
        )
    return order


# Which statuses message the customer, and with which strings/templates.
# IN_WASH / IN_DRY / IN_IRON are silent by design (owner-approved policy).
_STATUS_NOTIFICATIONS: dict[OrderStatus, tuple[str, str]] = {
    OrderStatus.READY: ("order_ready", "kk_order_ready"),
    OrderStatus.OUT_FOR_DELIVERY: ("order_out_for_delivery", "kk_order_out_for_delivery"),
    OrderStatus.DELIVERED: ("order_delivered", "kk_order_delivered"),
}


def _fmt_date(d: date) -> str:
    return d.strftime("%d %b %Y")


async def _notify_customer(
    db: AsyncSession,
    order: Order,
    *,
    message_key: str,
    template_name: str,
    template_params: list[str],
    **fmt: str,
) -> None:
    """Send a notification, degrading gracefully — NEVER raises.

    Chain: free-form (if 24h window open) -> registered template -> log-only.
    Called AFTER the business change is committed, so a send failure can
    never lose an order update.
    """
    try:
        customer = await db.get(Customer, order.customer_id)
        if customer is None or customer.opted_out or not customer.is_active:
            log.info(
                "notification_skipped",
                order_number=order.order_number,
                reason="opted_out_or_missing",
            )
            return
        text_body = get_message(message_key, order_number=order.order_number, **fmt)
        try:
            await send_message(db, to_phone=customer.phone, text=text_body)
        except WindowClosedError:
            await send_message(
                db,
                to_phone=customer.phone,
                template_name=template_name,
                template_params=template_params,
            )
    except SendError as exc:
        # Template not approved yet / Meta down — logged, business goes on.
        log.warning(
            "notification_failed",
            order_number=order.order_number,
            message_key=message_key,
            error=str(exc),
        )
    except Exception:
        log.exception(
            "notification_unexpected_error",
            order_number=order.order_number,
            message_key=message_key,
        )


async def _next_order_number(db: AsyncSession) -> str:
    """Mint KK-YYYYMMDD-NN under an advisory lock (concurrency-safe).

    The lock is transaction-scoped: it releases automatically at
    commit/rollback, so the caller's commit finalizes the claim.
    """
    await db.execute(text(f"SELECT pg_advisory_xact_lock({_ORDER_NUMBER_LOCK_KEY})"))
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"{ORDER_NUMBER_PREFIX}-{today}-"
    last = (
        await db.execute(
            select(Order.order_number)
            .where(Order.order_number.like(f"{prefix}%"))
            .order_by(Order.order_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    seq = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{seq:02d}"
