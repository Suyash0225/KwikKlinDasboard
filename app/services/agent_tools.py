"""Lookup + action tools the owner's assistant can call on its own.

Why this exists: a single fixed FACTS dump can only answer the questions we
thought of in advance. With these, the agent decides what it needs to know,
fetches it, and answers anything — a delivered order from last week, one
customer's whole history, who owes money, what a staff member last said.

Every tool returns a SHORT text block (the model reads it as an observation)
and never raises: a broken tool must degrade to "kuch nahi mila", not kill
the reply. Read-only by default; the one action tool (ping_staff) is the
owner's own instruction being carried out.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Conversation,
    Customer,
    Direction,
    Expense,
    Order,
    OrderStatus,
    OrderStatusHistory,
    Payment,
    PaymentStatus,
    Staff,
)
from app.services.messages import status_label
from app.utils.phone import normalize_phone

log = structlog.get_logger()

IST = timezone(timedelta(hours=5, minutes=30))

# What the model is told it can do. Keep descriptions concrete — vague tool
# docs are the #1 cause of a model guessing instead of looking up.
TOOL_SPECS = [
    {
        "name": "order_detail",
        "when": "owner asks about ONE order (KK-...): status, kab tak, kisne kya kiya, paisa",
        "args": "order_number",
    },
    {
        "name": "customer_detail",
        "when": "owner asks about a customer by name or phone: unke orders, kitna baaki, kab aaye the",
        "args": "query (naam ya number)",
    },
    {
        "name": "search_orders",
        "when": "owner asks for a LIST: late orders, aaj ke order, is hafte delivered, unpaid, kisi status ke",
        "args": "filter (late|today|week|unpaid|active|delivered|status:NAME)",
    },
    {
        "name": "staff_chat",
        "when": "owner asks what a staff member said / whether they replied / their pending work",
        "args": "name",
    },
    {
        "name": "money",
        "when": "owner asks paisa/revenue/kharcha/profit/outstanding for a period",
        "args": "period (today|week|month)",
    },
    {
        "name": "ping_staff",
        "when": "owner asks to message/remind/ask a staff member (haan ping karo, pucho, bol do)",
        "args": "name + message (message ko seedhe unse baat karte hue likho)",
    },
    {
        "name": "assign_task",
        "when": "owner gives someone WORK to do ('Ravi se bol do X ka order urgent hai', "
                "'Ajit ko bol do pickup karna hai') — banta hai trackable kaam, agent khud "
                "follow-up karega jab tak wo jawab na de",
        "args": "name | kaam (seedhe unse baat karte hue likho, 'pucho ki' mat likho)",
    },
    {
        "name": "task_list",
        "when": "owner asks what work is pending, kiska kaam baaki hai, kaun kya kar raha hai",
        "args": "open | done | staff naam (khali = sab open)",
    },
]


def _tool_help() -> str:
    return "\n".join(
        f"- {t['name']}({t['args']}): {t['when']}" for t in TOOL_SPECS
    )


def _money_fmt(v) -> str:
    return f"₹{Decimal(v or 0):.0f}"


def _items_line(items: list | None) -> str:
    parts = [f"{i.get('qty', 1)}x {i.get('type', '?')}" for i in (items or [])]
    return ", ".join(parts) or "-"


def _ist(dt: datetime | None) -> str:
    return dt.astimezone(IST).strftime("%d %b %H:%M") if dt else "?"


async def run_tool(db: AsyncSession, name: str, args: str) -> str:
    """Dispatch one tool call. Never raises."""
    fn = _TOOLS.get(name)
    if fn is None:
        return f"'{name}' naam ka koi tool nahi hai."
    try:
        out = await fn(db, (args or "").strip())
        log.info("agent_tool_ran", tool=name, args=args[:60], chars=len(out))
        return out or "Kuch nahi mila."
    except Exception:
        log.exception("agent_tool_failed", tool=name, args=args[:60])
        return f"'{name}' chalane mein dikkat aayi, data nahi mila."


# --------------------------------------------------------------------------
# read-only lookups
# --------------------------------------------------------------------------


async def _order_detail(db: AsyncSession, args: str) -> str:
    num = args.upper().strip()
    row = (
        await db.execute(
            select(Order, Customer)
            .join(Customer, Customer.id == Order.customer_id)
            .where(Order.order_number.ilike(f"%{num}%"))
            .limit(1)
        )
    ).first()
    if row is None:
        return f"{num} naam ka koi order nahi mila."
    o, c = row
    due = (o.total_amount or Decimal("0")) - (o.amount_paid or Decimal("0"))
    lines = [
        f"{o.order_number} | {c.name or c.phone} ({c.phone})",
        f"Status: {status_label(o.status)} | Priority: {o.priority}",
        f"Items: {_items_line(o.items)}",
        f"Bill {_money_fmt(o.total_amount)} | Mila {_money_fmt(o.amount_paid)} | "
        f"Baaki {_money_fmt(max(due, Decimal('0')))} ({o.payment_status.name})",
        f"Order bana: {_ist(o.created_at)} | Delivery promise: "
        f"{o.expected_delivery.strftime('%d %b') if o.expected_delivery else 'set nahi'}"
        + (f" | Deliver hua: {_ist(o.actual_delivery)}" if o.actual_delivery else ""),
    ]
    if o.notes:
        lines.append(f"Internal note: {o.notes[:200]}")

    hist = (
        (
            await db.execute(
                select(OrderStatusHistory)
                .where(OrderStatusHistory.order_id == o.id)
                .order_by(OrderStatusHistory.changed_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    if hist:
        lines.append("History (naya pehle):")
        lines += [
            f"  {_ist(h.changed_at)} -> {status_label(h.new_status)} (by {h.changed_by})"
            for h in hist
        ]
    pays = (
        (
            await db.execute(
                select(Payment).where(Payment.order_id == o.id).order_by(Payment.received_at)
            )
        )
        .scalars()
        .all()
    )
    if pays:
        lines.append(
            "Payments: "
            + ", ".join(
                f"{_money_fmt(p.amount)} {p.method.name} ({_ist(p.received_at)})" for p in pays
            )
        )
    return "\n".join(lines)


async def _customer_detail(db: AsyncSession, args: str) -> str:
    q = args.strip()
    if not q:
        return "Kis customer ke baare mein? Naam ya number batao."
    digits = "".join(ch for ch in q if ch.isdigit())
    cond = Customer.name.ilike(f"%{q}%")
    if len(digits) >= 6:
        cond = or_(cond, Customer.phone.ilike(f"%{digits}%"))
    custs = (
        (await db.execute(select(Customer).where(cond).limit(4))).scalars().all()
    )
    if not custs:
        return f"'{q}' naam/number ka koi customer nahi mila."
    if len(custs) > 1:
        return "Ek se zyada mile: " + ", ".join(
            f"{c.name or '(bina naam)'} {c.phone}" for c in custs
        ) + ". Kaun sa?"

    c = custs[0]
    orders = (
        (
            await db.execute(
                select(Order)
                .where(Order.customer_id == c.id)
                .order_by(Order.created_at.desc())
                .limit(8)
            )
        )
        .scalars()
        .all()
    )
    total_biz = sum((o.total_amount or Decimal("0")) for o in orders)
    due = sum(
        max((o.total_amount or Decimal("0")) - (o.amount_paid or Decimal("0")), Decimal("0"))
        for o in orders
    )
    lines = [
        f"{c.name or '(bina naam)'} | {c.phone}",
        f"Address: {c.address or 'nahi hai'}",
        f"Aakhri message: {_ist(c.last_message_at)}"
        + (" | STOP kiya hua hai" if c.opted_out else ""),
        f"Orders (aakhri {len(orders)}): business {_money_fmt(total_biz)}, baaki {_money_fmt(due)}",
    ]
    for o in orders:
        lines.append(
            f"  {o.order_number} | {status_label(o.status)} | {_money_fmt(o.total_amount)} "
            f"({o.payment_status.name}) | {_ist(o.created_at)}"
        )
    return "\n".join(lines)


async def _search_orders(db: AsyncSession, args: str) -> str:
    f = (args or "active").lower().strip()
    now = datetime.now(timezone.utc)
    today_ist = datetime.now(IST).date()
    q = select(Order, Customer).join(Customer, Customer.id == Order.customer_id)
    title = f

    from app.services.order_service import ACTIVE_STATUSES

    if f.startswith("status:"):
        name = f.split(":", 1)[1].upper().strip()
        try:
            q = q.where(Order.status == OrderStatus[name])
        except KeyError:
            return f"'{name}' koi status nahi hai."
    elif "late" in f or "der" in f:
        q = q.where(
            Order.expected_delivery < today_ist,
            Order.status.notin_((OrderStatus.DELIVERED, OrderStatus.CANCELLED)),
        )
        title = "late orders"
    elif "today" in f or "aaj" in f:
        start = datetime.combine(today_ist, datetime.min.time(), tzinfo=IST)
        q = q.where(Order.created_at >= start.astimezone(timezone.utc))
        title = "aaj ke orders"
    elif "week" in f or "hafte" in f:
        q = q.where(Order.created_at >= now - timedelta(days=7))
        title = "is hafte ke orders"
    elif "unpaid" in f or "baaki" in f or "udhaar" in f:
        q = q.where(
            Order.payment_status != PaymentStatus.PAID, Order.total_amount.isnot(None)
        )
        title = "jinka paisa baaki hai"
    elif "delivered" in f:
        q = q.where(Order.status == OrderStatus.DELIVERED)
        title = "delivered orders"
    else:
        q = q.where(Order.status.in_(ACTIVE_STATUSES))
        title = "active orders"

    rows = (await db.execute(q.order_by(Order.created_at.desc()).limit(25))).all()
    if not rows:
        return f"{title}: ek bhi nahi."
    lines = [f"{title} ({len(rows)}):"]
    for o, c in rows:
        due = (o.total_amount or Decimal("0")) - (o.amount_paid or Decimal("0"))
        lines.append(
            f"- {o.order_number} | {c.name or c.phone} | {status_label(o.status)} | "
            f"bill {_money_fmt(o.total_amount)}, baaki {_money_fmt(max(due, Decimal('0')))} | "
            f"delivery {o.expected_delivery.strftime('%d %b') if o.expected_delivery else '?'}"
        )
    return "\n".join(lines)


async def _staff_chat(db: AsyncSession, args: str) -> str:
    name = args.strip()
    q = select(Staff)
    if name:
        digits = "".join(ch for ch in name if ch.isdigit())
        cond = Staff.name.ilike(f"%{name}%")
        if len(digits) >= 6:
            cond = or_(cond, Staff.phone.ilike(f"%{digits}%"))
        q = q.where(cond)
    staff = (await db.execute(q.limit(4))).scalars().all()
    if not staff:
        return f"'{name}' naam ka koi staff nahi hai."

    out: list[str] = []
    for st in staff:
        msgs = (
            (
                await db.execute(
                    select(Conversation)
                    .where(Conversation.staff_id == st.id)
                    .order_by(Conversation.created_at.desc())
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
        out.append(f"{st.name} ({st.role.name}, {st.phone}):")
        if not msgs:
            out.append("  koi baat-cheet nahi hui")
            continue
        for m in reversed(msgs):
            who = st.name if m.direction is Direction.INBOUND else "hum"
            out.append(f"  [{_ist(m.created_at)}] {who}: {(m.message_text or '')[:120]}")
        if msgs[0].direction is Direction.OUTBOUND:
            gap = datetime.now(timezone.utc) - msgs[0].created_at
            hrs = int(gap.total_seconds() // 3600)
            out.append(
                f"  >> {st.name} ne humare aakhri message ka JAWAB NAHI diya "
                f"({hrs} ghante ho gaye)"
            )
        # what they're responsible for right now
        pending = (
            (
                await db.execute(
                    select(Order).where(
                        or_(
                            Order.assigned_washer_id == st.id,
                            Order.assigned_delivery_id == st.id,
                        ),
                        Order.status.notin_((OrderStatus.DELIVERED, OrderStatus.CANCELLED)),
                    )
                )
            )
            .scalars()
            .all()
        )
        if pending:
            out.append(
                "  Unke paas abhi: "
                + ", ".join(f"{o.order_number} ({status_label(o.status)})" for o in pending[:8])
            )
    return "\n".join(out)


async def _money_report(db: AsyncSession, args: str) -> str:
    period = (args or "month").lower().strip()
    now_ist = datetime.now(IST)
    if "today" in period or "aaj" in period:
        start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "aaj"
    elif "week" in period or "hafte" in period:
        start_ist = now_ist - timedelta(days=7)
        label = "pichhle 7 din"
    else:
        start_ist = now_ist.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        label = "is mahine"
    start = start_ist.astimezone(timezone.utc)

    collected = (
        await db.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.received_at >= start
            )
        )
    ).scalar_one()
    new_orders = (
        await db.execute(
            select(func.count()).select_from(Order).where(Order.created_at >= start)
        )
    ).scalar_one()
    billed = (
        await db.execute(
            select(func.coalesce(func.sum(Order.total_amount), 0)).where(
                Order.created_at >= start
            )
        )
    ).scalar_one()
    spent = (
        await db.execute(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.spent_on >= start_ist.date()
            )
        )
    ).scalar_one()
    outstanding = (
        await db.execute(
            select(func.coalesce(func.sum(Order.total_amount - Order.amount_paid), 0)).where(
                Order.total_amount.isnot(None),
                Order.payment_status != PaymentStatus.PAID,
            )
        )
    ).scalar_one()
    return "\n".join(
        [
            f"{label.capitalize()}:",
            f"- Naye order: {new_orders}, bill bana {_money_fmt(billed)}",
            f"- Paisa aaya (collection): {_money_fmt(collected)}",
            f"- Kharcha: {_money_fmt(spent)}",
            f"- Bacha (collection - kharcha): {_money_fmt(Decimal(collected) - Decimal(spent))}",
            f"Kul udhaar (sab time ka baaki): {_money_fmt(outstanding)}",
        ]
    )


def _money_fmt(v) -> str:
    return f"₹{Decimal(v or 0):.0f}"


# --------------------------------------------------------------------------
# action tool
# --------------------------------------------------------------------------


async def _ping_staff(db: AsyncSession, args: str) -> str:
    """Send a message to a staff member. args: 'Name | message'."""
    from app.services.whatsapp import SendError, send_message

    name, _, message = args.partition("|")
    name, message = name.strip(), message.strip()
    if not name or not message:
        return "Format: ping_staff('Naam | message')"

    staff = (
        (await db.execute(select(Staff).where(Staff.name.ilike(f"%{name}%")).limit(3)))
        .scalars()
        .all()
    )
    if len(staff) != 1:
        names = ", ".join(
            s.name for s in (await db.execute(select(Staff))).scalars().all() if s.name
        )
        return f"'{name}' saaf nahi hua. Staff hain: {names}"
    st = staff[0]
    body = f"📋 manager ki taraf se: {message}"
    try:
        await send_message(db, to_phone=st.phone, text=body, sent_by="bot")
    except SendError as exc:
        return f"{st.name} ko message nahi ja paya ({str(exc)[:80]}). Queue mein hai, retry hoga."
    return f"{st.name} ko bhej diya: {message}"


async def _assign_task(db: AsyncSession, args: str) -> str:
    """Give someone work and start tracking it. args: 'Name | what to do'."""
    from app.services import tasks as task_service

    name, _, what = args.partition("|")
    name, what = name.strip(), what.strip()
    if not name or not what:
        return "Format: assign_task('Naam | kya karna hai')"

    staff = await task_service.find_staff(db, name)
    if staff is None:
        names = ", ".join(
            s.name for s in (await db.execute(select(Staff))).scalars().all() if s.name
        )
        return f"'{name}' saaf nahi hua. Staff hain: {names}"

    import re as _re

    urgent = bool(_re.search(r"urgent|jaldi|jldi|turant|abhi|maang", what, _re.I))
    order = None
    m = _re.search(r"\bKK-\d{8}-\d{2,}\b", what, _re.I)
    if m:
        order = (
            await db.execute(select(Order).where(Order.order_number == m.group(0).upper()))
        ).scalar_one_or_none()

    task = await task_service.create_task(
        db, title=what, staff=staff, order=order, urgent=urgent, created_by="owner"
    )
    return (
        f"{staff.name} ko de diya [{task.code}]: {what}"
        + (" (URGENT)" if urgent else "")
        + ". Jawab na aane par main khud yaad dilata rahunga."
    )


async def _task_list(db: AsyncSession, args: str) -> str:
    from app.models import TASK_DONE, TASK_OPEN, Task

    f = (args or "open").strip().lower()
    q = select(Task).order_by(Task.created_at.desc()).limit(25)
    if f.startswith("done"):
        q = q.where(Task.status == TASK_DONE)
        title = "Ho chuke kaam"
    elif f in ("", "open", "pending", "baaki"):
        q = q.where(Task.status == TASK_OPEN)
        title = "Pending kaam"
    else:
        from app.services import tasks as task_service

        staff = await task_service.find_staff(db, f)
        if staff is None:
            return f"'{f}' naam ka koi staff nahi mila."
        q = q.where(Task.assigned_staff_id == staff.id, Task.status == TASK_OPEN)
        title = f"{staff.name} ke pending kaam"

    rows = (await db.execute(q)).scalars().all()
    if not rows:
        return f"{title}: ek bhi nahi."
    out = [f"{title} ({len(rows)}):"]
    for t in rows:
        who = "kisi ko nahi diya"
        if t.assigned_staff_id:
            st = await db.get(Staff, t.assigned_staff_id)
            who = st.name if st else "?"
        age_h = int((datetime.now(timezone.utc) - t.created_at).total_seconds() // 3600)
        bits = [f"- [{t.code}] {who}: {t.title[:80]}", f"({age_h}h purana"]
        if t.urgent:
            bits.append(", URGENT")
        if t.ping_count:
            bits.append(f", {t.ping_count} baar yaad dilaya")
        bits.append(")")
        line = bits[0] + " " + "".join(bits[1:])
        if t.reply:
            line += f"\n    unhone kaha: {t.reply[:90]}"
        out.append(line)
    return "\n".join(out)


_TOOLS = {
    "assign_task": _assign_task,
    "task_list": _task_list,
    "order_detail": _order_detail,
    "customer_detail": _customer_detail,
    "search_orders": _search_orders,
    "staff_chat": _staff_chat,
    "money": _money_report,
    "ping_staff": _ping_staff,
}
