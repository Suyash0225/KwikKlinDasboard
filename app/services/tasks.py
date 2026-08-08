"""Assigned work: create it, chase it, close it.

The owner says something once ("Ravi se bol do Sharma ji ka order urgent
hai"). From then on this module owns it: the staff member gets the message,
gets pinged every few hours until they answer, the manager hears about it if
they keep quiet, and whatever they finally say is written back onto the task
so the dashboard shows the truth instead of the last thing anyone remembered.
"""

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory
from app.models import TASK_CANCELLED, TASK_DONE, TASK_OPEN, Order, Staff, Task
from app.services import audit
from app.services.whatsapp import SendError, WindowClosedError, send_message

log = structlog.get_logger()

IST = timezone(timedelta(hours=5, minutes=30))

# how long we wait before nudging, and how many nudges before the manager hears
PING_AFTER_HOURS = 2
URGENT_PING_AFTER_HOURS = 1
ESCALATE_AFTER_PINGS = 3
QUIET_START, QUIET_END = settings.QUIET_HOURS_START, settings.QUIET_HOURS_END


def _in_quiet_hours(now_ist: datetime) -> bool:
    h = now_ist.hour
    if QUIET_START > QUIET_END:  # wraps midnight
        return h >= QUIET_START or h < QUIET_END
    return QUIET_START <= h < QUIET_END


async def _next_code(db: AsyncSession) -> str:
    """T-1, T-2, ... — short enough to type back on WhatsApp."""
    n = (await db.execute(select(func.count()).select_from(Task))).scalar_one()
    for candidate in range(n + 1, n + 50):
        code = f"T-{candidate}"
        clash = (
            await db.execute(select(Task.id).where(Task.code == code))
        ).scalar_one_or_none()
        if clash is None:
            return code
    raise RuntimeError("could not allocate a task code")


async def find_staff(db: AsyncSession, name_or_phone: str) -> Staff | None:
    """Match a staff member the way the owner refers to them."""
    q = (name_or_phone or "").strip()
    if not q:
        return None
    digits = "".join(ch for ch in q if ch.isdigit())
    cond = Staff.name.ilike(f"%{q}%")
    if len(digits) >= 6:
        cond = or_(cond, Staff.phone.ilike(f"%{digits}%"))
    rows = (await db.execute(select(Staff).where(cond).limit(2))).scalars().all()
    return rows[0] if len(rows) == 1 else None


async def create_task(
    db: AsyncSession,
    *,
    title: str,
    staff: Staff | None,
    order: Order | None = None,
    urgent: bool = False,
    created_by: str = "owner",
    notify: bool = True,
) -> Task:
    """Record the task and tell the assignee. Returns the saved Task."""
    task = Task(
        code=await _next_code(db),
        title=title.strip(),
        assigned_staff_id=staff.id if staff else None,
        order_id=order.id if order else None,
        urgent=urgent,
        created_by=created_by[:40],
    )
    db.add(task)
    await db.commit()

    if notify and staff is not None:
        sent = await _send_to_assignee(db, task, staff, first=True)
        if sent:
            task.last_ping_at = datetime.now(timezone.utc)
            db.add(task)
            await db.commit()

    await audit.record(
        actor_role="admin", actor=created_by, action="task_created",
        args={"code": task.code, "staff": staff.name if staff else None,
              "urgent": urgent},
        result=title[:150],
    )
    log.info("task_created", code=task.code, staff=staff.name if staff else None)
    return task


async def _send_to_assignee(
    db: AsyncSession, task: Task, staff: Staff, *, first: bool
) -> bool:
    """WhatsApp the assignee. False = could not deliver (logged, never raises)."""
    order_bit = ""
    if task.order_id:
        order = await db.get(Order, task.order_id)
        if order is not None:
            order_bit = f" ({order.order_number})"
    head = "🔴 URGENT" if task.urgent else "📋 Kaam"
    if first:
        body = (
            f"{head} [{task.code}]{order_bit}\n{task.title}\n\n"
            f"Ho jaye to reply karein: done {task.code}"
        )
    else:
        body = (
            f"⏰ Reminder [{task.code}]{order_bit}\n{task.title}\n\n"
            f"Kya status hai? Ho gaya ho to: done {task.code}"
        )
    try:
        await send_message(db, to_phone=staff.phone, text=body, sent_by="bot")
        return True
    except WindowClosedError:
        # Their 24h window is shut, so WhatsApp forbids free-form. Fall back
        # to the approved template with the task filled in — otherwise work
        # assigned in the evening would silently never reach them.
        one_line = " ".join(f"[{task.code}] {task.title}".split())[:600]
        try:
            await send_message(
                db, to_phone=staff.phone,
                template_name="kk_staff_alert", template_params=[one_line],
                sent_by="bot",
            )
            log.info("task_sent_via_template", code=task.code, staff=staff.name)
            return True
        except SendError:
            log.warning("task_template_failed", code=task.code, staff=staff.name)
            return False
    except SendError:
        log.warning("task_send_failed", code=task.code, staff=staff.name)
        return False


# --- pickup & delivery flow ------------------------------------------------
# Kapde lene jaana aur kapde dene jaana — dono ek hi baat-cheet hai, isliye
# ek hi code. Owner's rule (06 Aug): jab bhi koi pickup ya delivery bane,
# delivery boy se KHUD pucho, uska samay DB mein likho, aur owner ko batao.
#   1. ask the delivery boy "kab tak?"      (and FYI the owner)
#   2. his answer is stored on the task and told to the customer + owner
#   3. ask him "hua ya nahi?" with Yes/No, and move the order on Yes

JOB_KINDS = ("pickup", "delivery")

# Everything that differs between the two jobs lives here, nowhere else.
_JOB = {
    "pickup": {
        "emoji": "🧺",
        "word": "pickup",
        "title": "{who} ke yahan se pickup karna hai",
        "head": "Naya pickup",
        "ask": "Kab tak pickup kar loge? (jaise: sham tak / kal 11 baje)",
        "done_q": "Pickup ho gaya?",
        "yes_title": "✅ Haan, ho gaya",
        "customer_line": "Aapka pickup schedule ho gaya hai",
        "done_reply": "👍 Shukriya! Kapde aa gaye — main aage ka dekh leta hoon.",
    },
    "delivery": {
        "emoji": "🚚",
        "word": "delivery",
        "title": "{who} ko delivery karni hai",
        "head": "Delivery ke liye taiyar",
        "ask": "Kab tak deliver kar doge? (jaise: sham tak / kal 11 baje)",
        "done_q": "Delivery ho gayi?",
        "yes_title": "✅ Haan, ho gayi",
        "customer_line": "Aapke kapde delivery ke liye nikal rahe hain",
        "done_reply": "👍 Shukriya! Delivery mark kar di — customer ko bhi bata diya.",
    },
}


async def _delivery_staff(db: AsyncSession) -> Staff | None:
    """Who collects and delivers: the shop default, else the only DELIVERY."""
    from app.services import team

    return await team.delivery_staff(db)


async def _create_job_task(db: AsyncSession, order, kind: str) -> Task | None:
    """Ask the delivery boy about a pickup/delivery and track his answer.

    Never raises — a hiccup here must not undo a saved order. Idempotent:
    one open task per order per kind, so a status flip-flop cannot spam him.
    """
    from app.models import Customer

    cfg = _JOB[kind]
    try:
        existing = (
            await db.execute(
                select(Task).where(
                    Task.order_id == order.id,
                    Task.kind == kind,
                    Task.status == TASK_OPEN,
                )
            )
        ).scalars().first()
        if existing is not None:
            log.info("job_task_exists", kind=kind, code=existing.code)
            return existing

        staff = await _delivery_staff(db)
        if staff is None:
            log.info("job_task_skipped_no_delivery_staff", kind=kind, order=order.order_number)
            return None
        customer = await db.get(Customer, order.customer_id)
        who = (customer.name or customer.phone) if customer else "customer"
        addr = (customer.address if customer and customer.address else "").strip()

        task = Task(
            code=await _next_code(db),
            title=cfg["title"].format(who=who),
            assigned_staff_id=staff.id,
            order_id=order.id,
            kind=kind,
            urgent=order.priority == "urgent",
            created_by="agent",
        )
        db.add(task)
        await db.commit()

        ask = (
            f"{cfg['emoji']} {cfg['head']} [{task.code}] — {order.order_number}\n"
            f"{who} · {customer.phone if customer else ''}\n"
            + (f"Pata: {addr}\n" if addr else "")
            + f"\n{cfg['ask']}"
        )
        try:
            await send_message(db, to_phone=staff.phone, text=ask, sent_by="bot")
            task.last_ping_at = datetime.now(timezone.utc)
            db.add(task)
            await db.commit()
        except (SendError, WindowClosedError):
            log.info("job_ask_undelivered", kind=kind, code=task.code)

        # the owner side sees it the moment it is arranged, without asking
        from app.services import team

        await team.notify_admins(
            db,
            f"{cfg['emoji']} {order.order_number} — {who} ki {cfg['word']} "
            f"{staff.name} ko de di [{task.code}]. Unse samay pooch liya hai, "
            f"pata chalte hi bata dunga.",
        )

        await audit.record(
            actor_role="system", actor="agent", action=f"{kind}_task_created",
            args={"code": task.code, "order": order.order_number, "staff": staff.name},
            result="asked for ETA",
        )
        log.info("job_task_created", kind=kind, code=task.code, order=order.order_number)
        return task
    except Exception:
        log.exception("job_task_failed", kind=kind, order=getattr(order, "order_number", "?"))
        return None


async def create_pickup_task(db: AsyncSession, order) -> Task | None:
    """Called right after an order is booked."""
    return await _create_job_task(db, order, "pickup")


async def create_delivery_task(db: AsyncSession, order) -> Task | None:
    """Called the moment an order is READY — kapde taiyar hain, ab kab jaenge?"""
    return await _create_job_task(db, order, "delivery")


async def open_pickup_awaiting_eta(db: AsyncSession, staff_id) -> Task | None:
    """A pickup/delivery this person was asked about but has not answered."""
    return (
        await db.execute(
            select(Task)
            .where(
                Task.assigned_staff_id == staff_id,
                Task.status == TASK_OPEN,
                Task.kind.in_(JOB_KINDS),
                Task.eta_text.is_(None),
            )
            .order_by(Task.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def open_pickup_awaiting_confirm(db: AsyncSession, staff_id) -> Task | None:
    """Their newest pickup/delivery that is still open."""
    return (
        await db.execute(
            select(Task)
            .where(
                Task.assigned_staff_id == staff_id,
                Task.status == TASK_OPEN,
                Task.kind.in_(JOB_KINDS),
            )
            .order_by(Task.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def record_pickup_eta(db: AsyncSession, task: Task, eta_text: str) -> str:
    """Save what the boy promised, tell the customer AND the owner, then ask
    him to confirm once it is done. Returns the reply for the staff member."""
    from app.models import Customer, Order, OrderStatus
    from app.services import team

    cfg = _JOB.get(task.kind, _JOB["pickup"])
    task.eta_text = eta_text.strip()[:120]
    db.add(task)
    await db.commit()

    staff = await db.get(Staff, task.assigned_staff_id)
    order = await db.get(Order, task.order_id) if task.order_id else None
    customer = await db.get(Customer, order.customer_id) if order else None

    # the customer hears a time and a name, not "jald batayenge"
    if customer is not None:
        text = (
            f"Namaste{' ' + customer.name if customer.name else ''} ji! 🙏\n"
            f"{cfg['customer_line']} — *{task.eta_text}*.\n"
            f"{staff.name if staff else 'Hamare saathi'} aayenge, "
            f"unka number: {staff.phone if staff else ''}\n"
            f"Order: {order.order_number if order else ''}\n— Kwik Klin"
        )
        try:
            await send_message(db, to_phone=customer.phone, text=text, sent_by="bot")
        except WindowClosedError:
            log.info("job_eta_customer_window_closed", code=task.code)
        except SendError:
            log.warning("job_eta_customer_send_failed", code=task.code)

    # a pickup that has a time is a pickup somebody is going to make
    if task.kind == "pickup" and order is not None and order.status is OrderStatus.RECEIVED:
        try:
            from app.services import order_service

            await order_service.update_status(
                db, order, OrderStatus.PICKUP_ASSIGNED,
                changed_by=f"staff:{staff.name if staff else '?'}",
            )
        except Exception:
            log.exception("pickup_status_move_failed", code=task.code)

    # and the owner knows the promised time too
    await team.notify_admins(
        db,
        f"⏱ {staff.name if staff else 'Staff'} ne {cfg['word']} ke liye bola: "
        f"{task.eta_text} ({task.code}"
        + (f", {order.order_number}" if order else "")
        + "). Customer ko bata diya.",
    )

    await _ask_job_done(db, task, staff)
    return (
        f"👍 Note kar liya: {task.eta_text}. Customer ko bata diya.\n"
        f"{cfg['word'].capitalize()} ho jaye to niche wale button se bata dena."
    )


async def _ask_job_done(db: AsyncSession, task: Task, staff: Staff | None) -> None:
    """Yes/No buttons — one tap instead of typing."""
    from app.services.whatsapp import Button

    cfg = _JOB.get(task.kind, _JOB["pickup"])
    if staff is None:
        return
    try:
        await send_message(
            db, to_phone=staff.phone,
            text=f"[{task.code}] {cfg['done_q']}",
            buttons=[
                Button(f"job_yes:{task.code}", cfg["yes_title"]),
                Button(f"job_no:{task.code}", "❌ Abhi nahi"),
            ],
            sent_by="bot",
        )
    except (SendError, WindowClosedError):
        log.info("job_confirm_buttons_not_sent", code=task.code)


async def confirm_pickup(db: AsyncSession, task: Task, *, done: bool, by: str) -> str:
    """Their Yes/No answer.

    Yes on a pickup moves the order to PICKED_UP (which starts the delivery
    clock); yes on a delivery marks it DELIVERED. Either way the owner hears.
    """
    from app.models import Order, OrderStatus
    from app.services import team

    cfg = _JOB.get(task.kind, _JOB["pickup"])
    order = await db.get(Order, task.order_id) if task.order_id else None
    if not done:
        task.last_ping_at = datetime.now(timezone.utc)
        db.add(task)
        await db.commit()
        await team.notify_admins(
            db,
            f"⚠️ {by} ne bola {cfg['word']} abhi nahi hui ({task.code}"
            + (f", {order.order_number}" if order else "") + ").",
        )
        return "Theek hai, ho jaye to batana. Main thodi der baad phir poochh lunga."

    await complete_task(db, task, reply=f"{cfg['word']} ho gaya", by=by)
    if order is not None:
        try:
            from app.services import order_service

            if task.kind == "pickup":
                if order.status is OrderStatus.RECEIVED:
                    await order_service.update_status(
                        db, order, OrderStatus.PICKUP_ASSIGNED, changed_by=f"staff:{by}"
                    )
                if order.status is OrderStatus.PICKUP_ASSIGNED:
                    await order_service.update_status(
                        db, order, OrderStatus.PICKED_UP, changed_by=f"staff:{by}"
                    )
            else:
                if order.status is OrderStatus.READY:
                    await order_service.update_status(
                        db, order, OrderStatus.OUT_FOR_DELIVERY, changed_by=f"staff:{by}"
                    )
                if order.status is OrderStatus.OUT_FOR_DELIVERY:
                    await order_service.update_status(
                        db, order, OrderStatus.DELIVERED, changed_by=f"staff:{by}"
                    )
        except Exception:
            log.exception("job_done_status_move_failed", code=task.code)
    await team.notify_admins(
        db,
        f"✅ {by} ne {cfg['word']} kar di ({task.code}"
        + (f", {order.order_number}" if order else "") + ").",
    )
    return cfg["done_reply"]


async def complete_task(
    db: AsyncSession, task: Task, *, reply: str | None = None, by: str = "staff"
) -> Task:
    task.status = TASK_DONE
    task.completed_at = datetime.now(timezone.utc)
    if reply:
        task.reply = reply[:1000]
    db.add(task)
    await db.commit()
    await audit.record(
        actor_role="staff" if by != "dashboard" else "admin", actor=by,
        action="task_completed", args={"code": task.code}, result=(reply or "done")[:150],
    )
    log.info("task_completed", code=task.code, by=by)
    return task


async def cancel_task(db: AsyncSession, task: Task, *, by: str = "dashboard") -> Task:
    task.status = TASK_CANCELLED
    task.completed_at = datetime.now(timezone.utc)
    db.add(task)
    await db.commit()
    await audit.record(
        actor_role="admin", actor=by, action="task_cancelled",
        args={"code": task.code}, result="cancelled",
    )
    return task


async def get_by_code(db: AsyncSession, code: str) -> Task | None:
    code = (code or "").strip().upper()
    if not code.startswith("T-"):
        code = f"T-{code.lstrip('T-')}"
    return (
        await db.execute(select(Task).where(Task.code == code))
    ).scalar_one_or_none()


async def open_tasks_for_staff(db: AsyncSession, staff_id) -> list[Task]:
    return list(
        (
            await db.execute(
                select(Task)
                .where(Task.assigned_staff_id == staff_id, Task.status == TASK_OPEN)
                .order_by(Task.last_ping_at.desc().nullslast(), Task.created_at)
            )
        )
        .scalars()
        .all()
    )


async def note_reply(db: AsyncSession, staff_id, text: str) -> Task | None:
    """A staff member said something — attach it to the task they were last
    pinged about, so the dashboard shows their actual words."""
    tasks = await open_tasks_for_staff(db, staff_id)
    if not tasks:
        return None
    task = tasks[0]
    task.reply = text[:1000]
    db.add(task)
    await db.commit()
    return task


async def run_task_followups() -> int:
    """Scheduler entry: nudge assignees who owe an answer, escalate the
    stubborn ones to the manager. Returns how many messages went out."""
    now = datetime.now(timezone.utc)
    now_ist = datetime.now(IST)
    if _in_quiet_hours(now_ist):
        return 0

    sends = 0
    async with async_session_factory() as db:
        tasks = (
            (
                await db.execute(
                    select(Task)
                    .where(Task.status == TASK_OPEN, Task.assigned_staff_id.isnot(None))
                    .order_by(Task.created_at)
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        for task in tasks:
            gap_hours = URGENT_PING_AFTER_HOURS if task.urgent else PING_AFTER_HOURS
            since = task.last_ping_at or task.created_at
            if (now - since) < timedelta(hours=gap_hours):
                continue

            staff = await db.get(Staff, task.assigned_staff_id)
            if staff is None or not staff.is_active:
                continue

            if task.ping_count >= ESCALATE_AFTER_PINGS and task.escalated_at is None:
                waited = int((now - task.created_at).total_seconds() // 3600)
                try:
                    await send_message(
                        db, to_phone=settings.MANAGER_PHONE,
                        text=(
                            f"🚨 {staff.name} ne {task.code} ka jawab nahi diya "
                            f"({waited} ghante ho gaye).\n{task.title}\n"
                            f"Khud dekh lijiye ya kisi aur ko de dijiye."
                        ),
                    )
                    sends += 1
                except SendError:
                    log.info("task_escalation_not_sent", code=task.code)
                task.escalated_at = now
                db.add(task)
                await db.commit()
                continue

            if await _send_to_assignee(db, task, staff, first=False):
                sends += 1
            task.ping_count += 1
            task.last_ping_at = now
            db.add(task)
            await db.commit()

    if sends:
        log.info("task_followups_sent", count=sends)
    return sends
