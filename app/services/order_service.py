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

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Customer,
    Order,
    OrderStatus,
    OrderStatusHistory,
    PaymentMethod,
)
from app.services.messages import get_message
from app.services.whatsapp import Button, SendError, WindowClosedError, send_message
from app.utils.phone import normalize_phone

log = structlog.get_logger()

ORDER_NUMBER_PREFIX = "KK"
# Arbitrary constant identifying "order number generation" for the advisory
# lock. Any two transactions using the same key serialize on it.
_ORDER_NUMBER_LOCK_KEY = 834712

_SEQUENCE = [
    OrderStatus.RECEIVED,
    OrderStatus.PICKUP_ASSIGNED,
    OrderStatus.PICKED_UP,
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


class PlanLimitError(OrderError):
    """Plan ki limit khatam — galat request nahi, paisa ka mamla.

    Isliye ye alag exception hai: router ise 402 banata hai, 400 nahi.
    Feature gates pehle se 402 dete hain aur dashboard 402 par hi Upgrade
    prompt dikhata hai — order cap par 400 bhejne se owner ko sirf ek laal
    error dikhta tha, theek us waqt jab use upgrade ka rasta dikhna chahiye.
    """


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
    needs_pickup: bool = False,
    expected_delivery: date | None = None,
    notes: str | None = None,
    created_by: str = "system",
    # callers record the advance AFTER create; this makes the confirmation
    # message show the right advance/due numbers anyway (display only).
    advance_hint: Decimal | None = None,
    coupon_code: str | None = None,
    priority: str = "normal",
    bill_seconds: int | None = None,
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

    # Plan limit: is mahine ke orders (server-side enforcement — UI kuch
    # bhi kahe, yahan se aage nahi). None = unlimited.
    from app.services import plans as _plans
    from app.services import tenant_context

    _tid = tenant_context.current_tenant_id.get() or tenant_context.cached_home_tenant_id()
    if _tid is not None:
        from app.models.tenant import Tenant as _T

        _t = await db.get(_T, _tid)
        _limits = _plans.effective_limits(_t)
        _plan = _plans.get(_t.plan if _t else _plans.DEFAULT_PLAN)
        if _limits["max_orders_month"] is not None:
            from datetime import timezone as _tz

            _month_start = datetime.now(_tz.utc).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            _used = (
                await db.execute(
                    select(func.count()).select_from(Order).where(
                        Order.created_at >= _month_start, Order.tenant_id == _tid
                    )
                )
            ).scalar_one()
            if _used >= _limits["max_orders_month"]:
                nxt = _plans.next_plan_after(_plan.code)
                hint = f" {_plans.get(nxt).name} plan mein unlimited." if nxt else ""
                raise PlanLimitError(
                    f"Is mahine ki order limit ({_limits['max_orders_month']}) "
                    f"khatam — Upgrade karein.{hint}"
                )

    # Concurrency-safe upsert: two simultaneous creates for the same new
    # customer must not race — ON CONFLICT makes insert-or-skip atomic.
    # Core insert ORM ke before_flush stamp ko bypass karta hai, isliye
    # tenant_id yahan explicitly (RLS WITH CHECK bhi yahi maangta hai).

    await db.execute(
        pg_insert(Customer)
        .values(
            phone=phone,
            name=customer_name,
            tenant_id=tenant_context.effective_tenant_id(),
        )
        # Grahak ab per-dukaan unique hai: ek hi number do alag laundry ka
        # customer ho sakta hai. Conflict bhi usi jodi par dekhna hoga.
        .on_conflict_do_nothing(index_elements=["tenant_id", "phone"])
    )
    customer = (
        await db.execute(select(Customer).where(Customer.phone == phone))
    ).scalar_one()
    if customer_name and not customer.name:
        customer.name = customer_name

    # Coupon yahin, order banne aur grahak ko message jaane se PEHLE. Pehle
    # router ise create ke baad lagata tha: message purane (bina chhoot ke)
    # total ke saath chala jaata tha, aur galat coupon par 400 aata tha
    # jabki bill ban chuka hota tha.
    coupon, coupon_discount = None, Decimal("0")
    if coupon_code:
        from app.services.marketing_agent import validate_coupon

        coupon, coupon_discount, err = await validate_coupon(
            db, coupon_code, customer.id, total_amount or Decimal("0")
        )
        if err:
            await db.rollback()
            raise OrderError(f"coupon: {err}")
        total_amount = (total_amount or Decimal("0")) - coupon_discount
        discount_amount = (discount_amount or Decimal("0")) + coupon_discount

    order_number = await _next_order_number(db)

    order = Order(
        order_number=order_number,
        customer_id=customer.id,
        status=OrderStatus.PICKUP_ASSIGNED if needs_pickup else OrderStatus.RECEIVED,
        items=items,
        total_amount=total_amount,
        discount_amount=discount_amount,
        gst_amount=gst_amount,
        pickup_date=pickup_date,
        expected_delivery=expected_delivery,
        notes=notes,
        priority="urgent" if priority == "urgent" else "normal",
    )
    from app.services.turnaround import clean_bill_seconds

    order.bill_seconds = clean_bill_seconds(bill_seconds)
    db.add(order)
    await db.flush()
    db.add(
        OrderStatusHistory(
            order_id=order.id,
            old_status=None,
            new_status=OrderStatus.PICKUP_ASSIGNED if needs_pickup else OrderStatus.RECEIVED,
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
    if coupon is not None:
        from app.services.marketing_agent import redeem_coupon

        await redeem_coupon(db, coupon, order, coupon_discount)

    # lead -> customer (Marketing Agent pipeline; never raises)
    try:
        from app.services.leads import mark_converted

        await mark_converted(db, phone)
    except Exception:
        log.exception("lead_convert_hook_failed")

    from app.services import payment_offers as _payment_offers
    payment_offer = None
    try:
        payment_offer = await _payment_offers.create_offer(
            db, order=order, kind="advance", created_by=created_by
        )
    except Exception:
        log.exception("advance_payment_offer_failed", order_number=order_number)

    # Pickup confirmation WITH the bill details (owner's policy 03 Aug).
    from app.services.work_orders import items_summary

    items_text = items_summary(order)
    advance_amt = advance_hint if advance_hint is not None else (order.amount_paid or Decimal("0"))
    total_s = f"{order.total_amount}" if order.total_amount is not None else "—"
    if order.total_amount is not None and order.discount_amount:
        # Chhoot total ke saath hi — isse Meta ka approved template (fixed
        # 7 params), owner ka apna format aur default copy, teeno bina
        # badle discount dikhate hain.
        total_s += f" (₹{order.discount_amount} discount)"
    advance_s = f"{advance_amt}"
    due = (order.total_amount or Decimal("0")) - advance_amt
    due_s = f"{max(due, 0)}" if order.total_amount is not None else "—"
    date_s = _fmt_date(expected_delivery) if expected_delivery else "to be confirmed soon"
    from app.services import bill_link

    await _notify_customer(
        db, order,
        message_key=(
            "order_confirmed_bill" if order.total_amount is not None
            else "order_confirmed_no_price"
        ),
        template_name="kk_bill_details",
        template_params=[
            customer_name or "there", order_number, items_text[:120],
            total_s, advance_s, due_s, date_s,
        ],
        items=items_text,
        total=total_s,
        advance=advance_s,
        due=due_s,
        date=date_s,
        # web bill + GPay/PhonePe se payment (services/bill_link.py)
        bill_line=bill_link.message_line(_payment_offers.url_for_offer(await bill_link.url_for(db, order), payment_offer) if payment_offer is not None else await bill_link.url_for(db, order)),
        payment_offer=payment_offer,
    )

    # The owner side hears about every new order without asking — unless he
    # is the one who just booked it (no point echoing his own message back).
    try:
        await _notify_admins_fyi(
            db, created_by,
            f"🧾 Naya order {order_number} — {customer.name or phone}\n"
            f"{items_text[:120]}\nBill ₹{total_s}"
            + (f", advance ₹{advance_s}" if advance_amt else "")
            + f"\nDelivery: {date_s} · banaya: {created_by}",
        )
    except Exception:
        log.exception("order_admin_fyi_failed", order_number=order_number)
    # Kapde grahak ke ghar hain — delivery wale ko lene jaana hai.
    #
    # Pehle har bill RECEIVED banta tha, yaani "kapde dukaan mein hain".
    # Delivery wale ka panel sirf PICKUP_ASSIGNED / READY / OUT_FOR_DELIVERY
    # dikhata hai, isliye counter par bana koi bhi bill uske paas kabhi
    # pahunchta hi nahi tha — chahe wo phone par aaya order ho jise lene
    # jaana hai. `pickup_date` field maujood thi par sirf store hoti thi,
    # kisi cheez par asar nahi karti thi.
    # Ops agent: washerman + (pickup ho to) delivery boy chunkar order-linked
    # kaam bana deta hai. Pickup task khud boy se "kab tak?" poochta hai,
    # isliye tab alag work-order WhatsApp nahi — ek hi baat do baar nahi.
    from app.services import ops_agent

    assigned = await ops_agent.on_order_created(db, order)
    if needs_pickup and not assigned["pickup"]:
        try:
            from app.services.work_orders import send_work_order

            await send_work_order(
                db, order,
                headline=f"🛵 Pickup: {customer.name or phone}",
                extra=((customer.address or "").strip() or "Address customer se poochein"),
                role="DELIVERY",
            )
        except Exception:
            # Kaam panel mein to dikh hi jayega — WhatsApp na jaana order
            # banne se nahi rok sakta.
            log.exception("pickup_work_order_failed", order_number=order_number)
    # Loyalty: laga hua reward "used", aur naya inaam bana to grahak ko khabar
    from app.services import rewards

    await rewards.on_order_created(db, order, customer, coupon)
    _announce(order, "created", by=created_by)
    return order


def _announce(order, action: str, *, by: str = "") -> None:
    """Khuli hui screens ko ishara: is order par kuch hua.

    Yahi wo cheez thi jo Ajit ke banaye bill par nahi chali — bill DB mein
    theek baitha tha, par owner ka khula hua dashboard usse poochta hi
    nahi tha. Ab wo khud bata deta hai.

    Kabhi raise nahi karta: ek bhi order, ek bhi payment live-update ki
    wajah se nahi girna chahiye.
    """
    try:
        from app.services import events

        events.publish(
            order.tenant_id, "order",
            number=order.order_number, action=action, by=by,
        )
    except Exception:      # noqa: BLE001
        log.debug("order_event_skipped", order_number=getattr(order, "order_number", "?"))


# Actors that ARE the owner side — telling them what they just did is noise.
_OWNER_ACTORS = {"manager", "dashboard", "admin"}


async def _notify_admins_fyi(db: AsyncSession, actor: str, text: str) -> None:
    """FYI to the admins, skipped when the admin himself did it."""
    if (actor or "").strip().lower() in _OWNER_ACTORS:
        return
    from app.services import team

    await team.notify_admins(db, text)


async def update_status(
    db: AsyncSession,
    order: Order,
    new_status: OrderStatus,
    *,
    changed_by: str,
    notify: bool = True,
) -> Order:
    """Move an order through the state machine. Commits.

    Raises InvalidTransitionError on a move the machine forbids.
    notify=False: grahak ko status wala message nahi (jab caller apna,
    zyada saaf message bhej raha ho — jaise partial delivery).
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
    if new_status is OrderStatus.PICKED_UP:
        # SLA clock starts at pickup (owner's Order Agent spec): 4 din
        # normal, 7 din heavy items. Computed BEFORE the commit below so
        # status + promise land atomically — a crash can't leave a picked-up
        # order without its delivery date.
        from app.services import app_settings

        heavy_words = [
            w.strip().lower()
            for w in str(await app_settings.get(db, "heavy_items")).split(",")
            if w.strip()
        ]
        items_blob = " ".join(
            str(i.get("type", "")) + " " + str(i.get("service", ""))
            for i in (order.items or [])
        ).lower()
        heavy = any(w in items_blob for w in heavy_words)
        days = int(
            await app_settings.get(
                db, "sla_heavy_days" if heavy else "sla_normal_days"
            )
        )
        order.expected_delivery = date.today() + timedelta(days=days)
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
    _announce(order, "status", by=changed_by)
    from app.services import ops_agent

    await ops_agent.on_status_change(db, order, new_status, changed_by)

    # Kapde taiyar = ab delivery ka sawaal. Owner's rule (06 Aug): delivery
    # boy se turant pucho "kab tak?", jawab DB mein rakho, owner ko batao.
    # Best-effort: a hiccup here must never undo a committed status change.
    if new_status is OrderStatus.READY:
        try:
            from app.services.tasks import create_delivery_task

            await create_delivery_task(db, order)
        except Exception:
            log.exception("delivery_task_hook_failed", order_number=order.order_number)

    if not notify:
        return order

    if new_status is OrderStatus.PICKED_UP:
        await _notify_customer(
            db, order,
            message_key="pickup_done",
            template_name="kk_picked_up",
            template_params=[order.order_number, _fmt_date(order.expected_delivery)],
            count=str(sum(int(i.get("qty", 1)) for i in (order.items or []))),
            date=_fmt_date(order.expected_delivery),
        )
        return order

    if new_status is OrderStatus.DELIVERED:
        payment_offer = None
        try:
            from app.services import payment_offers as _payment_offers
            payment_offer = await _payment_offers.create_offer(
                db, order=order, kind="reminder", created_by=changed_by
            )
        except Exception:
            log.exception("delivery_payment_offer_failed", order_number=order.order_number)

        # thank-you + rating buttons (owner's policy 03 Aug)
        await _notify_customer(
            db, order,
            message_key="thankyou_rating",
            template_name="kk_thankyou_rating",
            template_params=[order.order_number],
            payment_offer=payment_offer,
            buttons=[
                Button("rate_good", "⭐ Excellent"),
                Button("rate_mid", "🙂 It was okay"),
                Button("rate_bad", "😞 Needs work"),
            ],
        )
        return order
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
    notify_customer: bool = True,
) -> Order:
    """Add a received payment; payment_status derives from the model rule.

    notify_customer=False sirf booking ke advance par: wahan "order received"
    message mein advance pehle se likha hota hai — do message nahi.

    Writes an append-only Payment ledger row (source of truth for reports)
    AND updates orders.amount_paid (derived cache the rest of the app reads).
    """
    if amount <= 0:
        raise OrderError("payment amount must be positive")

    offer_applied = None
    try:
        from app.models import PaymentOffer
        from datetime import datetime, timezone
        candidate = (
            await db.execute(
                select(PaymentOffer)
                .where(
                    PaymentOffer.order_id == order.id,
                    PaymentOffer.tenant_id == order.tenant_id,
                    PaymentOffer.offer_amount == amount,
                )
                .order_by(PaymentOffer.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        current_due = (order.total_amount or Decimal("0")) - (order.amount_paid or Decimal("0"))
        now = datetime.now(timezone.utc)
        if (
            candidate is not None
            and current_due == candidate.original_amount
            and candidate.opened_at is not None
            and candidate.expires_at is not None
            and candidate.opened_at <= now < candidate.expires_at
        ):
            order.total_amount = (order.total_amount or Decimal("0")) - candidate.discount_amount
            order.discount_amount = (order.discount_amount or Decimal("0")) + candidate.discount_amount
            offer_applied = candidate
            log.info("payment_offer_applied", order_number=order.order_number, offer=candidate.offer_code, discount=str(candidate.discount_amount))
    except Exception:
        log.exception("payment_offer_apply_failed", order_number=order.order_number)
    # Overpayment is ALLOWED — customers round up or pay advance for the next
    # order (owner's rule, 04 Aug). Log it so a genuine typo is still visible.
    if order.total_amount is not None:
        outstanding = order.total_amount - (order.amount_paid or Decimal("0"))
        if amount > outstanding:
            log.info(
                "payment_over_outstanding",
                order_number=order.order_number,
                amount=str(amount),
                outstanding=str(outstanding),
            )
    from app.models import Payment, PaymentStatus  # local import avoids cycle noise

    db.add(
        Payment(
            order_id=order.id,
            amount=amount,
            method=method,
            recorded_by=recorded_by[:80],
            note=((note or "") + (f" | offer:{offer_applied.offer_code}" if offer_applied else "")).strip() or None,
        )
    )
    order.amount_paid = ((order.amount_paid or Decimal("0")) + amount).quantize(
        Decimal("0.01")
    )
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

    # Paisa aaya — owner ko turant pata chale (jab tak usne khud na likha ho).
    due = (order.total_amount or Decimal("0")) - (order.amount_paid or Decimal("0"))
    try:
        await _notify_admins_fyi(
            db, recorded_by,
            f"💰 {order.order_number} — ₹{amount} {method.name} mila"
            + (f" ({note[:60]})" if note else "")
            + f".\nAb tak ₹{order.amount_paid or 0}, baaki ₹{max(due, Decimal('0'))} "
            f"({order.payment_status.name}) · likha: {recorded_by}",
        )
    except Exception:
        log.exception("payment_admin_fyi_failed", order_number=order.order_number)
    # Grahak ko bhi — "₹300 mil gaya, baaki ₹200". Dashboard, staff collect,
    # WhatsApp bot: paisa jahan se bhi likha jaye, grahak ko pakki raseed.
    if notify_customer:
        from app.services.customer_messages import balance_line, rupees

        amount_s, balance = rupees(amount), balance_line(order)
        await _notify_customer(
            db, order,
            message_key="payment_thanks",
            template_name="kk_payment_received",
            template_params=[amount_s, order.order_number, balance],
            amount=amount_s,
            balance_line=balance,
        )
    _announce(order, "payment", by=recorded_by)
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
    buttons: list[Button] | None = None,
    payment_offer=None,
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
        # Dukaan ka APNA naam — pehle .env ka SHOP_NAME jaata tha, yaani har
        # doosri dukaan ke grahak ko "— Kwik Klin" likha message milta.
        if "shop" not in fmt:
            from app.models.tenant import Tenant

            tenant = await db.get(Tenant, order.tenant_id) if order.tenant_id else None
            if tenant is not None and (tenant.shop_name or "").strip():
                fmt["shop"] = tenant.shop_name.strip()
        fmt.setdefault("name", (customer.name or "").strip() or "there")
        # Bill ka link har grahak-message mein: free text mein {bill_line},
        # template par "View bill" button (registry mein url_button). Grahak
        # ko har baar wahi page — status, partial delivery, paid — sab live.
        from app.services import bill_link
        from app.services.templates import TEMPLATES

        bill_url = await bill_link.url_for(db, order)
        if payment_offer is not None:
            from app.services import payment_offers as _payment_offers
            bill_url = _payment_offers.url_for_offer(bill_url, payment_offer)
        if "bill_line" not in fmt:
            fmt["bill_line"] = bill_link.message_line(bill_url)
        url_param = None
        if TEMPLATES.get(template_name, {}).get("url_button") and order.tenant_id:
            url_param = bill_url.rsplit("/b/", 1)[-1] if "/b/" in bill_url else (order.bill_code or bill_link.make(order.tenant_id, order.id))
        text_body = get_message(message_key, order_number=order.order_number, **fmt)

        # WAHA button messages use URL buttons for the customer-facing
        # actions. Meta keeps its existing reply-button semantics.
        outbound_buttons = buttons
        if settings.WHATSAPP_PROVIDER == "waha":
            actions: list[Button] = []
            if bill_url:
                if payment_offer is not None:
                    actions.append(Button("pay_now", "💳 Pay Now", "url", bill_url))
                elif order.total_amount is not None and (order.total_amount - (order.amount_paid or Decimal("0"))) > 0:
                    actions.append(Button("pay_now", "💳 Pay Now", "url", bill_url))
                actions.append(Button("view_bill", "🧾 View Bill", "url", bill_url))
            # Keep the delivered rating action as a reply button so the
            # existing deterministic rating handler remains intact.
            for b in (buttons or []):
                if b.id.startswith("rate_"):
                    actions.append(b)
            # WAHA cannot expose Meta's approved template quick replies, so
            # where the delivered message has a configured Google review URL,
            # expose it as a direct Review button. Meta keeps its existing
            # rating quick replies unchanged.
            if message_key == "thankyou_rating":
                try:
                    from app.services import customer_messages
                    review_links = await customer_messages.review_links(db)
                    if review_links:
                        link = review_links[sum(ord(c) for c in customer.phone) % len(review_links)]
                        if link:
                            actions = [b for b in actions if not b.id.startswith("rate_")]
                            actions.append(Button("review", "⭐ Review", "url", link))
                except Exception:
                    log.exception("review_button_link_failed", order_number=order.order_number)
            outbound_buttons = actions[:3]

        try:
            await send_message(
                db, to_phone=customer.phone, text=text_body, buttons=outbound_buttons
            )
        except SendError as exc:
            # WAHA documents Send Buttons as deprecated/fragile. If the
            # interactive endpoint rejects a button payload, retry the same
            # business message as plain text with the secure HTTPS bill link.
            # Never retry after a transient failure: that could duplicate a
            # successfully accepted message whose response was lost.
            if settings.WHATSAPP_PROVIDER == "waha" and outbound_buttons and not exc.transient:
                log.warning(
                    "waha_buttons_fallback_to_text",
                    order_number=order.order_number,
                    error=str(exc),
                )
                await send_message(
                    db, to_phone=customer.phone, text=text_body, buttons=None
                )
            else:
                raise
        except WindowClosedError:
            await send_message(
                db,
                to_phone=customer.phone,
                template_name=template_name,
                template_params=template_params,
                template_url_param=url_param,
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
