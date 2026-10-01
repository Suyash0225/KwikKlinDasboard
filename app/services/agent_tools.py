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
    {
        "name": "add_expense",
        "when": "owner says money was SPENT ('100 ka petrol dala', 'bijli ka bill 2000 "
                "diya', 'salary de di') — tum khud kharcha likhte ho, kisi ko bolna nahi hai",
        "args": "amount | kis cheez ka (jaise '100 | petrol')",
    },
    {
        "name": "add_customer",
        "when": "owner says save/add a customer ('is number ko X ke naam se save kar do') "
                "— tum khud database me daalte ho, ye kaam kisi staff ka nahi hai",
        "args": "phone | naam",
    },
    {
        "name": "set_shop_info",
        "when": "owner tells you a SHOP detail to remember: khulne-band hone ka time, "
                "dukaan ka address, contact number, ya default delivery din "
                "('add kro office khulne ka time 11 se 6 ka hai') — customers ko "
                "jawab dete waqt yahi use hota hai",
        "args": "timing|address|phone|turnaround | value",
    },
]

# Tools that CHANGE data. The assistant may only claim something is done if
# one of these actually ran — see bill_agent's unbacked-claim check.
WRITE_TOOLS = {
    "add_expense", "add_customer", "set_shop_info", "assign_task", "ping_staff",
}


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


def _parse_task_schedule(text: str, *, now_ist: datetime | None = None) -> tuple[datetime | None, str | None]:
    """Parse an owner-friendly pickup/task date into an IST due_at.

    Supports ISO dates, '5 October', '5 Oct 2026', 'Monday 5 October',
    and a weekday by itself ('next Monday'). If a weekday and date are both
    supplied they must agree; the agent must never silently move a task to a
    different day.
    """
    import calendar as _calendar
    import re as _re

    now = now_ist or datetime.now(IST)
    raw = " ".join((text or "").split())
    low = raw.lower()
    months = {m.lower(): i for i, m in enumerate(_calendar.month_name) if m}
    months.update({m.lower(): i for i, m in enumerate(_calendar.month_abbr) if m})
    weekdays = {
        "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
        "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3,
        "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
    }

    target_date = None
    weekday_name = None

    iso = _re.search(r"(?<!\\d)(20\\d{2})-(\\d{1,2})-(\\d{1,2})(?!\\d)", low)
    if iso:
        try:
            target_date = datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), tzinfo=IST).date()
        except ValueError:
            return None, f"Date '{iso.group(0)}' valid nahi hai."

    if target_date is None:
        dm = _re.search(
            r"(?<!\\d)(\\d{1,2})(?:st|nd|rd|th)?\\s+"
            r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
            r"(?:\\s+(20\\d{2}))?(?!\\w)",
            low,
        )
        if dm:
            month_text = dm.group(2)
            month = next((n for name, n in months.items() if name == month_text or month_text.startswith(name[:3])), None)
            if month is None:
                return None, f"Month '{month_text}' samajh nahi aaya."
            year = int(dm.group(3)) if dm.group(3) else now.year
            try:
                candidate = datetime(year, month, int(dm.group(1)), tzinfo=IST).date()
            except ValueError:
                return None, f"Date '{dm.group(0)}' valid nahi hai."
            if dm.group(3) is None and candidate < now.date():
                candidate = datetime(year + 1, month, int(dm.group(1)), tzinfo=IST).date()
            target_date = candidate

    for word, wd in weekdays.items():
        if _re.search(rf"(?<!\\w){_re.escape(word)}(?!\\w)", low):
            weekday_name = word
            break

    if target_date is None and weekday_name is not None:
        days_ahead = (weekdays[weekday_name] - now.date().weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        target_date = now.date() + timedelta(days=days_ahead)

    if target_date is None:
        return None, None

    if weekday_name is not None and target_date.weekday() != weekdays[weekday_name]:
        actual = target_date.strftime("%A")
        wanted = weekday_name.title()
        return None, f"{target_date.strftime('%d %b %Y')} {actual} hai, {wanted} nahi. Date/weekday dobara check karo."

    # Optional clock time. If only a date was supplied, 09:00 IST is the
    # operational task time; the exact date remains the source of truth.
    tm = _re.search(r"(?<!\\d)(\\d{1,2})(?::(\\d{2}))?\\s*(am|pm)(?!\\w)", low)
    if tm:
        hour = int(tm.group(1))
        minute = int(tm.group(2) or 0)
        if not 1 <= hour <= 12:
            return None, "Time valid nahi hai."
        if tm.group(3) == "pm" and hour != 12:
            hour += 12
        if tm.group(3) == "am" and hour == 12:
            hour = 0
    else:
        hour, minute = 9, 0

    return datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=IST), None


def _extract_customer_details(text: str) -> tuple[str | None, str | None]:
    """Extract an explicit customer phone + name without guessing from dates."""
    import re as _re

    raw = " ".join((text or "").split())
    phone_m = _re.search(r"(?<!\\d)(?:\\+91[\\s-]?)?[6-9]\\d{9}(?!\\d)", raw)
    phone = phone_m.group(0) if phone_m else None

    cleaned = raw
    if phone_m:
        cleaned = cleaned[:phone_m.start()] + " " + cleaned[phone_m.end():]
    cleaned = _re.sub(r"(?i)\\b(customer|grahak)\\b", " ", cleaned)
    cleaned = _re.sub(r"(?i)\\b(no|number|phone|mobile)\\b", " ", cleaned)
    cleaned = _re.sub(r"(?i)\\b(pick[- ]?up|pickup|pick|collect|task|kaam|urgent|jaldi|jldi|turant|abhi)\\b", " ", cleaned)
    cleaned = _re.sub(
        r"(?i)\\b(?:on|for|at)\\s+(?:monday|mon|tuesday|tue|tues|wednesday|wed|"
        r"thursday|thu|thur|friday|fri|saturday|sat|sunday|sun)\\b", " ", cleaned
    )
    cleaned = _re.sub(
        r"(?i)\\b(?:\\d{1,2})(?:st|nd|rd|th)?\\s+"
        r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\\s+20\\d{2})?\\b",
        " ", cleaned,
    )
    cleaned = _re.sub(r"(?i)\\b(?:on|for|at)\\b", " ", cleaned)
    cleaned = _re.sub(r"[^A-Za-z .'-]", " ", cleaned)
    name = " ".join(cleaned.split()).strip(" .-'")
    return phone, (name or None)


async def _assign_task(db: AsyncSession, args: str) -> str:
    """Create a real tracked task from natural owner language.

    Accepted examples:
      'Ajit | pickup for Uma Shankar Agrwal customer no +919839983077 on Monday 5 October'
      'Ajit | pickup | Uma Shankar Agrwal | +919839983077 | Monday 5 October'
    """
    from app.services import tasks as task_service
    from app.models import TASK_OPEN, Task as _Task

    import re as _re

    parts = [p.strip() for p in (args or "").split("|") if p.strip()]
    name = parts[0] if parts else ""
    what = " | ".join(parts[1:]).strip()
    if not name or not what:
        return "Format: assign_task('Ajit | pickup | customer name | phone | date')"

    staff = await task_service.find_staff(db, name)
    if staff is None:
        names = ", ".join(
            s.name for s in (await db.execute(select(Staff))).scalars().all() if s.name
        )
        return f"'{name}' saaf nahi hua. Staff hain: {names}"

    low = what.lower()
    kind = "general"
    if _re.search(r"\\bpick[- ]?up\\b|\\bcollect(?:ion)?\\b", low):
        kind = "pickup"
    elif _re.search(r"\\bdeliver(?:y)?\\b", low):
        kind = "delivery"
    elif _re.search(r"\\bwash(?:ing)?\\b", low):
        kind = "wash"
    elif _re.search(r"\\bdry(?:ing)?\\b", low):
        kind = "dry"
    elif _re.search(r"\\biron(?:ing)?\\b", low):
        kind = "iron"

    urgent = bool(_re.search(r"\\burgent\\b|\\bjaldi\\b|\\bjldi\\b|\\bturant\\b|\\babhi\\b", low))
    due_at, date_error = _parse_task_schedule(what)
    if date_error:
        return f"Task nahi banaya: {date_error}"

    phone, customer_name = _extract_customer_details(what)
    # Pipe-delimited input is even safer: the model can explicitly provide
    # customer name/phone/date instead of relying on sentence cleanup.
    if len(parts) >= 3:
        explicit = " ".join(parts[2:])
        p2, n2 = _extract_customer_details(explicit)
        phone = phone or p2
        customer_name = n2 or customer_name

    customer = None
    if phone:
        try:
            normalized = normalize_phone(phone)
        except ValueError:
            return f"Customer number '{phone}' valid mobile number nahi lag raha."
        customer = (
            await db.execute(select(Customer).where(Customer.phone == normalized))
        ).scalar_one_or_none()
        if customer is None:
            if not customer_name:
                return f"{normalized} database mein nahi mila. Customer ka naam bhi batao."
            customer = Customer(phone=normalized, name=customer_name[:120])
            db.add(customer)
            await db.flush()
        elif customer_name and not customer.name:
            customer.name = customer_name[:120]

    # If a name was supplied without a phone, only use it when exactly one
    # active customer matches. Never attach a task to the wrong similarly-named
    # customer.
    if customer is None and customer_name:
        rows = (
            await db.execute(
                select(Customer).where(
                    Customer.is_active.is_(True),
                    Customer.name.ilike(f"%{customer_name}%"),
                ).limit(5)
            )
        ).scalars().all()
        if len(rows) == 1:
            customer = rows[0]
        elif len(rows) > 1:
            return "Customer naam se ek se zyada mile. Phone number bhi batao."

    order = None
    if customer is not None:
        from app.services.order_service import ACTIVE_STATUSES
        active_orders = (
            await db.execute(
                select(Order)
                .where(Order.customer_id == customer.id, Order.status.in_(ACTIVE_STATUSES))
                .order_by(Order.created_at.desc())
                .limit(10)
            )
        ).scalars().all()
        if len(active_orders) == 1:
            order = active_orders[0]

    title_who = customer.name if customer is not None and customer.name else (customer.phone if customer else "customer")
    action_word = {
        "pickup": "Pickup",
        "delivery": "Delivery",
        "wash": "Wash",
        "dry": "Dry",
        "iron": "Iron",
        "general": "Task",
    }[kind]
    title = f"{action_word} — {title_who}"
    if due_at:
        title += f" — {due_at.astimezone(IST).strftime('%d %b %Y')}"
    # Preserve useful free-form instructions while removing obvious metadata.
    instruction = _re.sub(r"(?i)\\b(customer|grahak|no|number|phone|mobile)\\b", " ", what)
    instruction = " ".join(instruction.split()).strip(" |")
    if instruction and instruction.lower() not in {kind, action_word.lower()}:
        title += f": {instruction[:350]}"

    # Idempotency: repeating the same owner instruction must not create two
    # pickups for the same customer/staff/day.
    if customer is not None and due_at is not None:
        day_start = due_at.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        existing = (
            await db.execute(
                select(_Task).where(
                    _Task.assigned_staff_id == staff.id,
                    _Task.customer_id == customer.id,
                    _Task.kind == kind,
                    _Task.status == TASK_OPEN,
                    _Task.due_at >= day_start.astimezone(timezone.utc),
                    _Task.due_at < day_end.astimezone(timezone.utc),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return f"Ye task pehle se bana hua hai [{existing.code}]: {existing.title}"

    task = await task_service.create_task(
        db,
        title=title,
        staff=staff,
        order=order,
        customer=customer,
        urgent=urgent,
        created_by="owner",
        kind=kind,
        due_at=due_at,
    )
    return (
        f"{staff.name} ko de diya [{task.code}]: {title}"
        + (" (URGENT)" if urgent else "")
        + (f". Customer: {customer.name or customer.phone}" if customer else "")
        + (f". Date: {due_at.astimezone(IST).strftime('%A, %d %b %Y')}" if due_at else "")
        + (f". Running order {order.order_number} bhi link kar diya." if order else "")
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


# An expense the agent writes must land in the same buckets the Expenses
# page and the reports already chart — services/expenses.py owns that list.
_CATEGORY_HINTS = (
    ("Transport", ("petrol", "diesel", "fuel", "gaadi", "gadi", "bike", "scooty",
                   "auto", "rickshaw", "tempo", "transport", "delivery", "van")),
    ("Detergent", ("detergent", "surf", "soap", "sabun", "powder", "chemical",
                   "starch", "bleach", "neel", "kharid")),
    ("Electricity", ("bijli", "electric", "current", "light bill", "meter")),
    ("Rent", ("rent", "kiraya", "kiraaya", "kiray")),
    ("Salary", ("salary", "tankhwah", "tankhwa", "pagar", "wages", "majduri", "mazduri")),
    ("Maintenance", ("repair", "marammat", "maintenance", "machine", "mistri",
                     "service", "spare", "parts")),
)


def _guess_category(text: str) -> str:
    low = text.lower()
    for cat, words in _CATEGORY_HINTS:
        if any(w in low for w in words):
            return cat
    return "Other"


async def _add_expense(db: AsyncSession, args: str) -> str:
    """Write a real expense row. args: 'amount | kis cheez ka'."""
    import re as _re

    head, _, rest = args.partition("|")
    note = rest.strip() or head.strip()
    # The amount field must BE an amount. Loosely grabbing the first digit it
    # could find turned "time 11 se 6 settings me add kar do" into a ₹11
    # expense — a junk row in the owner's books is worse than a refusal.
    bare = _re.sub(r"(?i)\b(rs\.?|inr|rupa?ye?|rupees?)\b|[₹,]", "", head).strip()
    if _re.fullmatch(r"\d+(?:\.\d+)?", bare):
        amount = Decimal(bare)
    else:
        nums = set(_re.findall(r"\d+(?:\.\d+)?", args))
        if len(nums) != 1:
            return (
                "Kitne rupaye ka kharcha hua? Format: add_expense('100 | petrol'). "
                "Agar ye kharcha hai hi nahi to ye tool mat chalao."
            )
        amount = Decimal(nums.pop())
    if amount <= 0:
        return "Kharcha 0 se zyada hona chahiye."

    from app.services.expenses import canonical_category

    # owner ki apni categories bhi — "packaging | 200" seedha usi khaane mein
    cat = await canonical_category(db, note) or _guess_category(note or args)
    today = datetime.now(IST).date()
    exp = Expense(
        category=cat,
        amount=amount,
        spent_on=today,
        description=note[:300] or None,
    )
    db.add(exp)
    await db.commit()

    month_total = (
        await db.execute(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.spent_on >= today.replace(day=1)
            )
        )
    ).scalar_one()
    log.info("agent_expense_added", amount=str(amount), category=cat)
    return (
        f"Kharcha likh diya: {_money_fmt(amount)} — {cat}"
        + (f" ({note[:60]})" if note else "")
        + f", {today.strftime('%d %b')}. Is mahine ka kul kharcha ab "
        f"{_money_fmt(month_total)} hai."
    )


async def _add_customer(db: AsyncSession, args: str) -> str:
    """Create (or name) a customer. args: 'phone | naam' — either order."""
    parts = [p.strip() for p in args.split("|") if p.strip()]
    phone_raw, name = "", ""
    for p in parts:
        digits = "".join(ch for ch in p if ch.isdigit())
        if len(digits) >= 10 and not phone_raw:
            phone_raw = digits
        elif not name:
            name = p
    if not phone_raw:
        return "Number chahiye. Format: add_customer('9984601311 | Rahul Shah')"

    try:
        phone = normalize_phone(phone_raw)
    except ValueError:
        return f"'{phone_raw}' sahi mobile number nahi lag raha."

    existing = (
        await db.execute(select(Customer).where(Customer.phone == phone))
    ).scalar_one_or_none()
    if existing is not None:
        if not name or (existing.name or "").lower() == name.lower():
            return f"{existing.name or 'Ye number'} ({phone}) pehle se database mein hai."
        was = existing.name
        existing.name = name[:120]
        await db.commit()
        log.info("agent_customer_renamed", phone=phone, name=name)
        return (
            f"{phone} ka naam {name} save kar diya"
            + (f" (pehle '{was}' tha)." if was else ".")
        )

    cust = Customer(phone=phone, name=(name[:120] or None))
    db.add(cust)
    await db.commit()
    log.info("agent_customer_added", phone=phone, name=name)
    return f"Naya customer save kar diya: {name or '(bina naam)'} — {phone}."


# Shop facts the owner may set by WhatsApp. Deliberately NOT here: UPI VPA,
# GST% and anything else where a misheard word costs money or breaks the
# books — those stay on the Settings page.
_SHOP_FIELDS = {
    "timing": "shop_hours", "timings": "shop_hours", "time": "shop_hours",
    "hours": "shop_hours", "samay": "shop_hours", "khulne": "shop_hours",
    "address": "shop_address", "pata": "shop_address", "location": "shop_address",
    "phone": "shop_contact_phone", "number": "shop_contact_phone",
    "contact": "shop_contact_phone", "mobile": "shop_contact_phone",
    "turnaround": "turnaround_days", "delivery": "turnaround_days",
    "days": "turnaround_days", "din": "turnaround_days",
}
_FIELD_LABEL = {
    "shop_hours": "Shop ka time",
    "shop_address": "Shop ka address",
    "shop_contact_phone": "Shop ka contact number",
    "turnaround_days": "Default delivery din",
}


async def _set_shop_info(db: AsyncSession, args: str) -> str:
    """Store a shop detail the customer-facing bot will use. args: 'field | value'."""
    from app.services import app_settings

    field, _, value = args.partition("|")
    field, value = field.strip().lower(), value.strip()
    if not value:
        return (
            "Kya value set karni hai? Format: "
            "set_shop_info('timing | subah 11 se shaam 6, Sunday band')"
        )
    key = _SHOP_FIELDS.get(field) or next(
        (k for word, k in _SHOP_FIELDS.items() if word in field), None
    )
    if key is None:
        return (
            "Ye main abhi set nahi kar sakta. Sirf ye kar sakta hoon: timing, "
            "address, phone, turnaround. Baaki Settings page se hota hai."
        )

    if key == "turnaround_days":
        import re as _re

        m = _re.search(r"\d+", value)
        if not m or not (1 <= int(m.group(0)) <= 30):
            return "Delivery din 1 se 30 ke beech hone chahiye."
        stored: object = int(m.group(0))
        shown = f"{stored} din"
    else:
        stored = value[:300]
        shown = str(stored)

    await app_settings.set_value(db, key, stored)
    log.info("agent_shop_info_set", key=key)
    return (
        f"{_FIELD_LABEL[key]} save kar diya: {shown}. "
        "Ab customer poochhega to bot yahi batayega."
    )


_TOOLS = {
    "assign_task": _assign_task,
    "add_expense": _add_expense,
    "add_customer": _add_customer,
    "set_shop_info": _set_shop_info,
    "task_list": _task_list,
    "order_detail": _order_detail,
    "customer_detail": _customer_detail,
    "search_orders": _search_orders,
    "staff_chat": _staff_chat,
    "money": _money_report,
    "ping_staff": _ping_staff,
}
