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
from app.models import TASK_CANCELLED, TASK_DONE, TASK_OPEN, Order, Staff, StaffRole, Task
from app.services import app_settings, audit
from app.services.whatsapp import SendError, WindowClosedError, send_message
from app.services.tenant_context import manager_phone

log = structlog.get_logger()

IST = timezone(timedelta(hours=5, minutes=30))

# how long we wait before nudging, and how many nudges before the manager hears
PING_AFTER_HOURS = 2
URGENT_PING_AFTER_HOURS = 1
ESCALATE_AFTER_PINGS = 3
MAX_FOLLOWUPS_PER_DAY = 3
QUIET_START, QUIET_END = settings.QUIET_HOURS_START, settings.QUIET_HOURS_END

# Operational teams share the same queue. assigned_staff_id remains the
# primary/notification owner, but every active member of the relevant team
# can see and update the open task. This keeps the DB source of truth while
# making the WhatsApp/dashboard workflow collaborative.
SHARED_TEAM_ROLES = {"WASHER", "SUPERVISOR", "DELIVERY"}
SHARED_JOB_KINDS = {"pickup", "delivery"}

def _task_team_matches_staff(task: Task, staff: Staff) -> bool:
    role = staff.role.name
    if role in {"ADMIN", "MANAGER"}:
        return True
    if role == "DELIVERY":
        return task.kind in SHARED_JOB_KINDS
    if role in {"WASHER", "SUPERVISOR"}:
        return task.kind not in SHARED_JOB_KINDS
    return False

async def staff_can_access_task(db: AsyncSession, task: Task, staff: Staff) -> bool:
    """Shared-team authorization; manager/admin always have full access."""
    if not staff.is_active:
        return False
    if _task_team_matches_staff(task, staff):
        return True
    return task.assigned_staff_id == staff.id



def _announce(task: Task, action: str, *, by: str = "") -> None:
    """Khuli hui screens ko bata do ki is task ka kya hua.

    Sirf ISHARA jaata hai — code, kya hua, kisne kiya. Task ka poora
    matter nahi: har screen wahi maangti hai jo wo dikha rahi hai, aur
    kisi ka data galat connection par ja hi nahi sakta.

    Ye kabhi raise nahi karta. Live update suvidha hai; uski wajah se ek
    bhi task band hona ya cancel hona nahi rukna chahiye.
    """
    try:
        from app.services import events

        events.publish(
            task.tenant_id, "task", code=task.code, action=action, by=by,
        )
    except Exception:      # noqa: BLE001
        log.debug("task_event_skipped", code=getattr(task, "code", "?"))


def _in_quiet_hours(now_ist: datetime) -> bool:
    h = now_ist.hour
    if QUIET_START > QUIET_END:  # wraps midnight
        return h >= QUIET_START or h < QUIET_END
    return QUIET_START <= h < QUIET_END


_TASK_CODE_LOCK_KEY = 834713


async def _next_code(db: AsyncSession) -> str:
    """T-1, T-2, ... — short enough to type back on WhatsApp.

    Transaction-scoped lock: ops agent ab har bill par kaam banata hai, aur
    ek saath do bill bane to dono ko wahi code milta tha (uq_tasks_tenant_code).
    Lock caller ke commit par khud chhoot jaata hai — order number jaisa.
    """
    from sqlalchemy import text as _text

    await db.execute(_text(f"SELECT pg_advisory_xact_lock({_TASK_CODE_LOCK_KEY})"))
    rows = (await db.execute(select(Task.code))).scalars().all()
    used_numbers = {
        int(code[2:])
        for code in rows
        if isinstance(code, str) and code.startswith("T-") and code[2:].isdigit()
    }
    n = max(used_numbers, default=0)
    for candidate in range(n + 1, n + 50):
        code = f"T-{candidate}"
        clash = (
            await db.execute(select(Task.id).where(Task.code == code))
        ).scalar_one_or_none()
        if clash is None:
            return code
    raise RuntimeError("could not allocate a task code")


async def find_staff(db: AsyncSession, name_or_phone: str) -> Staff | None:
    """Match a staff member the way the owner refers to them.

    Do niyam jo ise rozmarra mein aasan banate hain:

    - Sirf ACTIVE log. Jise owner ne band kar diya use naya kaam nahi jata.
    - Poora naam/number bola ho to wahi jeetta hai. Pehle koi bhi do
      mel khate rows (do "Ravi", ya "Ravi" aur "Ravindra") milte hi function
      haar maan leta tha aur owner ko "staff list mein nahi mila" dikhta
      tha — chahe usne poora naam theek likha ho.
    """
    q = (name_or_phone or "").strip()
    if not q:
        return None
    digits = "".join(ch for ch in q if ch.isdigit())
    cond = Staff.name.ilike(f"%{q}%")
    if len(digits) >= 6:
        cond = or_(cond, Staff.phone.ilike(f"%{digits}%"))
    rows = (
        await db.execute(select(Staff).where(cond, Staff.is_active.is_(True)))
    ).scalars().all()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        return None
    exact = [
        s
        for s in rows
        if s.name.strip().casefold() == q.casefold()
        or (len(digits) >= 6 and "".join(ch for ch in s.phone if ch.isdigit()) == digits)
    ]
    return exact[0] if len(exact) == 1 else None


async def create_task(
    db: AsyncSession,
    *,
    title: str,
    staff: Staff | None,
    order: Order | None = None,
    customer=None,
    urgent: bool = False,
    created_by: str = "owner",
    notify: bool = True,
    kind: str = "general",
    due_at: datetime | None = None,
) -> Task:
    """Record the task and tell the assignee. Returns the saved Task.

    Pickup/delivery tasks are always tied to a real order/customer. A job task
    without that relationship cannot be safely followed up or completed.
    Generic staff tasks may still exist without an order.
    """
    if kind in {"pickup", "wash", "dry", "iron", "delivery"} and order is None and customer is None:
        raise ValueError(f"{kind} task requires a customer or order")
    if order is not None and customer is not None and order.customer_id != customer.id:
        raise ValueError("task customer does not match the selected order")

    linked_customer_id = order.customer_id if order is not None else (
        customer.id if customer is not None else None
    )
    task = Task(
        code=await _next_code(db),
        title=title.strip(),
        assigned_staff_id=staff.id if staff else None,
        order_id=order.id if order else None,
        # For order-linked tasks, the order is the source of truth for the
        # customer. Never allow a task to point at a different customer.
        customer_id=linked_customer_id,
        urgent=urgent,
        created_by=created_by[:40],
        kind=kind,
        due_at=due_at,
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
    _announce(task, "created", by=created_by)
    return task


async def send_task_to(db: AsyncSession, task: Task, staff: Staff) -> bool:
    """Naye aadmi ko kaam bhejo (reassign ke baad)."""
    sent = await _send_to_assignee(db, task, staff, first=True)
    if sent:
        task.last_ping_at = datetime.now(timezone.utc)
        db.add(task)
        await db.commit()
    return sent


async def _send_to_assignee(
    db: AsyncSession, task: Task, staff: Staff, *, first: bool
) -> bool:
    """WhatsApp the assignee. False = could not deliver (logged, never raises)."""
    if not staff.ai_agent_enabled:
        return False
    order_bit = ""
    order_details = ""
    order = None
    if task.order_id:
        order = await db.get(Order, task.order_id)
    if order is not None:

            order_bit = f" ({order.order_number})"
            # Staff ko sirf task title nahi, kaam karne ke liye zaroori context
            # bhi mile. Link signed hai; amount/details DB se hi aate hain.
            try:
                from app.models import Customer
                from app.services.bill_link import url_for as bill_url_for
                from app.services.work_orders import items_summary
                customer = await db.get(Customer, order.customer_id)
                due = max(
                    (order.total_amount or 0) - (order.amount_paid or 0), 0
                )
                lines = [
                    f"👤 Customer: {(customer.name if customer and customer.name else customer.phone if customer else 'Customer')}",
                    f"🧺 Items: {items_summary(order)}",
                ]
                if order.expected_delivery:
                    lines.append(f"📅 Delivery: {order.expected_delivery.strftime('%d %b %Y')}")
                if task.due_at:
                    lines.append(f"⏰ Task due: {task.due_at.astimezone(IST).strftime('%d %b %Y, %I:%M %p')}")
                if task.kind in ("pickup", "delivery") and customer and customer.address:
                    lines.append(f"📍 Address: {customer.address.strip()}")
                # Financial information is owner/manager-only. Task
                # WhatsApp messages to operational staff must never expose
                # customer dues or payment links.
                if staff.role in (StaffRole.MANAGER, StaffRole.ADMIN):
                    if due > 0:
                        lines.append(f"💰 Due: ₹{due:g}")
                    bill_link = await bill_url_for(db, order)
                    if bill_link:
                        lines.append(f"🧾 Bill & payment: {bill_link}")
                order_details = "\n" + "\n".join(lines) + "\n"
            except Exception:
                log.exception("task_order_context_failed", code=task.code)
    if order is None and task.customer_id:
        try:
            from app.models import Customer
            customer = await db.get(Customer, task.customer_id)
            if customer is not None:
                order_details = f"\n👤 Customer: {customer.name or 'Customer'}\n📱 WhatsApp: {customer.phone}\n"
                if task.kind == "pickup" and customer.address:
                    order_details += f"📍 Address: {customer.address.strip()}\n"
        except Exception:
            log.exception("task_customer_context_failed", code=task.code)

    language = str(await app_settings.get(db, "communication_language") or "en").lower()
    if language not in ("en", "hi"):
        language = "en"
    if language == "hi":
        head = "🔴 URGENT" if task.urgent else "📋 KAAM ASSIGNMENT"
        if task.kind == "wash": head = "🧺 WASHING KAAM"
        elif task.kind == "dry": head = "💨 DRYING KAAM"
        elif task.kind == "iron": head = "👔 IRONING KAAM"
        elif task.kind == "pickup": head = "🛵 PICKUP KAAM"
        elif task.kind == "delivery": head = "🚚 DELIVERY KAAM"
        complete_line = "Neeche diye gaye status menu se current status select karein."
        update_line = "Please status menu se current task status update karein."
    else:
        head = "🔴 URGENT" if task.urgent else "📋 TASK ASSIGNMENT"
        if task.kind == "wash": head = "🧼 WASHING TASK"
        elif task.kind == "dry": head = "💨 DRYING TASK"
        elif task.kind == "iron": head = "👔 IRONING TASK"
        elif task.kind == "pickup": head = "🧺 PICKUP TASK"
        elif task.kind == "delivery": head = "🚚 DELIVERY TASK"
        complete_line = "Select the current task status from the menu below."
        update_line = "Please use the status menu to keep the task updated."
    if first:
        body = (
            f"{head} [{task.code}]{order_bit}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{task.title}\n"
            f"{order_details}\n"
            f"{complete_line}\n"
            f"— Kwik Klin"
        )
    else:
        reminder_head = "⏰ KAAM KA REMINDER"
        if order is not None and order.expected_delivery is not None:
            days_left = (order.expected_delivery - datetime.now(IST).date()).days
            if days_left <= 0:
                reminder_head = "🚨 DELIVERY DEADLINE — URGENT REMINDER"
            elif days_left == 1:
                reminder_head = "🟠 DELIVERY KAL — URGENT REMINDER"
            elif days_left == 2:
                reminder_head = "🟡 DELIVERY 2 DIN MEIN — REMINDER"
        body = (
            f"{reminder_head} [{task.code}]{order_bit}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{task.title}\n"
            f"{order_details}\n"
            f"{update_line}\n"
            f"— Kwik Klin"
        )
    # Fixed choices should be a WhatsApp list, not free-text instructions.
    # The menu is role-aware: washer/supervisor gets Wash/Iron/Ready/Pending;
    # delivery gets Done/Pending for both pickup and delivery. The task code
    # stays in every row id, so a tap can never update the wrong task.
    try:
        from app.services.work_orders import task_status_menu

        rows = await task_status_menu(db, task.code)
        if rows:
            menu_body = body + "\n\nPlease select the current task status from the menu below."
            await send_message(
                db,
                to_phone=staff.phone,
                text=menu_body,
                list_rows=rows,
                list_button="Update status",
                list_title="Task status",
                sent_by="bot",
            )
        else:
            await send_message(db, to_phone=staff.phone, text=body, sent_by="bot")
        log.info("task_whatsapp_assignment_sent", code=task.code, staff=staff.name)
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
        "yes_title": "✅ Pickup done",
        "customer_line": "Your pickup is scheduled",
        "done_reply": "👍 Shukriya — thank you. Pickup marked as done.",
    },
    "delivery": {
        "emoji": "🚚",
        "word": "delivery",
        "title": "{who} ko delivery karni hai",
        "head": "Delivery ke liye taiyar",
        "ask": "Kab tak deliver kar doge? (jaise: sham tak / kal 11 baje)",
        "done_q": "Delivery ho gayi?",
        "yes_title": "✅ Delivered",
        "customer_line": "Your clothes are on the way",
        "done_reply": "🙏 Shukriya! Delivery marked as done. Customer updated.",
    },
}


async def _delivery_staff(db: AsyncSession) -> Staff | None:
    """Who collects and delivers: the shop default, else the only DELIVERY."""
    from app.services import team

    return await team.delivery_staff(db)


# "Kab tak?" par jaan-boojh kar koi button NAHI.
# Baaki har jagah button hain (ho gaya / time lagega / dikkat hai) kyunki
# wahan jawab gine-chune hote hain. Samay aisa nahi hai: delivery wala
# haath ka kaam dekh kar batata hai — "1-2 ghante / sham tak / kal" jaise
# chhape hue option na uske kaam se milte hain na owner ko sach dikhate
# hain. Yahan uska apna jawab hi sahi jawab hai.


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

        # Ops agent: order par laga boy, warna sabse kam kaam wala; agent band
        # ho to purana niyam (default / akela delivery boy)
        from app.services import ops_agent

        if await ops_agent.enabled(db):
            staff = await ops_agent.pick_staff(db, "DELIVERY", order)
        else:
            staff = await _delivery_staff(db)
        if staff is None:
            log.info("job_task_skipped_no_delivery_staff", kind=kind, order=order.order_number)
            return None
        if order.assigned_delivery_id is None:
            order.assigned_delivery_id = staff.id
            db.add(order)
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
            + (f"📍 Address: {addr}\n" if addr else "")
            + f"\n\n{cfg['ask']}\nPlease select the current task status from the menu below."
        )
        delivered = "no"
        try:
            from app.services.work_orders import task_status_menu
            rows = await task_status_menu(db, task.code)
            await send_message(
                db,
                to_phone=staff.phone,
                text=ask,
                list_rows=rows,
                list_button="Update status",
                list_title=cfg["word"].title(),
                sent_by="bot",
            )
            delivered = "yes"
        except WindowClosedError:
            # Unki 24h chat band hai — free-form Meta allow nahi karta. Ye
            # chupchaap chhod dena sabse bura tha: owner ko "de di, samay
            # pooch liya" chala jata tha jabki Ajit tak kuch pahuncha hi
            # nahi hota. Ab approved template se jata hai.
            one_line = " ".join(ask.split())[:600]
            try:
                await send_message(
                    db, to_phone=staff.phone, template_name="kk_staff_alert",
                    template_params=[one_line], sent_by="bot",
                )
                delivered = "template"
            except SendError:
                log.warning("job_ask_undelivered", kind=kind, code=task.code)
        except SendError:
            log.warning("job_ask_send_failed", kind=kind, code=task.code)
        if delivered != "no":
            task.last_ping_at = datetime.now(timezone.utc)
            db.add(task)
            await db.commit()

        # the owner side sees it the moment it is arranged, without asking —
        # aur sach-sach: pahuncha ya nahi, ye bhi
        from app.services import team

        tail = {
            "yes": "Task assign kar diya hai aur status menu bhej diya hai.",
            "template": (
                f"Unki chat band thi, isliye template se bheja hai — "
                f"jawab aate hi bata dunga."
            ),
            "no": (
                f"⚠️ Par {staff.name} tak message NAHI pahuncha (chat band + "
                f"template bhi fail). Unhe khud bata dijiye."
            ),
        }[delivered]
        await team.notify_admins(
            db,
            f"{cfg['emoji']} {order.order_number} — {who} ki {cfg['word']} "
            f"{staff.name} ko de di [{task.code}]. {tail}",
        )

        await audit.record(
            actor_role="system", actor="agent", action=f"{kind}_task_created",
            args={"code": task.code, "order": order.order_number, "staff": staff.name},
            result="asked for task status",
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


async def _staff_open_task_filter(db: AsyncSession, staff_id):
    staff = await db.get(Staff, staff_id)
    if staff is None:
        return None
    if staff.role.name == "DELIVERY":
        return (Task.kind.in_(JOB_KINDS))
    if staff.role.name in {"WASHER", "SUPERVISOR"}:
        return (Task.kind.notin_(JOB_KINDS))
    return (Task.assigned_staff_id == staff_id)

async def open_pickup_awaiting_eta(db: AsyncSession, staff_id) -> Task | None:
    """Newest shared delivery-team pickup/delivery awaiting an ETA."""
    cond = await _staff_open_task_filter(db, staff_id)
    if cond is None:
        return None
    return (
        await db.execute(
            select(Task)
            .where(cond, Task.status == TASK_OPEN, Task.kind.in_(JOB_KINDS), Task.eta_text.is_(None))
            .order_by(Task.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def open_pickup_awaiting_confirm(db: AsyncSession, staff_id) -> Task | None:
    """Newest shared delivery-team pickup/delivery still open."""
    cond = await _staff_open_task_filter(db, staff_id)
    if cond is None:
        return None
    return (
        await db.execute(
            select(Task)
            .where(cond, Task.status == TASK_OPEN, Task.kind.in_(JOB_KINDS))
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
            f"Hello{' ' + customer.name if customer.name else ''}! 🙏\n"
            f"{cfg['customer_line']} — *{task.eta_text}*.\n"
            f"{staff.name if staff else 'Our team member'} will come"
            + (f", contact: {staff.phone}\n" if staff else ".\n") +
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
    language = str(await app_settings.get(db, "communication_language") or "en").lower()
    if language == "hi":
        done_title = "✅ Haan, ho gaya" if task.kind == "pickup" else "✅ Haan, ho gayi"
        pending_title = "⏳ Pending"
        question = cfg["done_q"]
    else:
        done_title = cfg["yes_title"]
        pending_title = "⏳ Pending"
        question = cfg["done_q"]
    try:
        from app.services.whatsapp import ListRow

        await send_message(
            db, to_phone=staff.phone,
            text=f"📋 {cfg['word'].upper()} TASK [{task.code}]\\n{question}",
            list_rows=[
                ListRow(f"job_yes:{task.code}", done_title, "Mark the task completed"),
                ListRow(f"job_no:{task.code}", pending_title, "Task is still pending"),
            ],
            list_button="Update status",
            list_title=cfg["word"].title(),
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
    if task.status != TASK_OPEN:
        return f"ℹ️ *{task.code}* already complete ho chuka hai. Pehla valid update accept hua tha."
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

    # A delivery task has two distinct customer-visible milestones. The first
    # Done tap means the parcel has LEFT the shop; keep the task open so the
    # delivery worker can confirm the actual hand-off with a second Done tap.
    # Never let one tap cascade READY -> OUT_FOR_DELIVERY -> DELIVERED.
    if task.kind == "delivery" and order is not None:
        from app.services import order_service

        if order.status is OrderStatus.READY:
            try:
                await order_service.update_status(
                    db, order, OrderStatus.OUT_FOR_DELIVERY, changed_by=f"staff:{by}"
                )
            except Exception:
                log.exception("job_out_for_delivery_status_move_failed", code=task.code)
                return "Delivery status update nahi ho paya. Please manager ko batayein."
            task.last_ping_at = datetime.now(timezone.utc)
            db.add(task)
            await db.commit()
            await team.notify_admins(
                db,
                f"🚚 {by} ne {order.order_number} ko out for delivery mark kiya "
                f"({task.code}). Delivery complete hone par dobara Done karein.",
            )
            return "🚚 Order out for delivery mark ho gaya. Customer ko update bhej diya hai. Delivery complete hone ke baad is task par Done dobara dabayein."

        if order.status is not OrderStatus.OUT_FOR_DELIVERY:
            return (
                f"⚠️ {order.order_number} abhi {order.status.name} hai. "
                "Delivery task sirf READY ya OUT_FOR_DELIVERY order par complete ho sakta hai."
            )

    completed = await complete_task(db, task, reply=f"{cfg['word']} ho gaya", by=by)
    if not getattr(completed, "_completion_won", False):
        return f"ℹ️ *{task.code}* already complete ho chuka hai. Pehla valid update accept hua tha."

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
            elif task.kind == "delivery" and order.status is OrderStatus.OUT_FOR_DELIVERY:
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
    db: AsyncSession, task: Task, *, reply: str | None = None, by: str = "staff",
    advance_order: bool = True,
) -> Task:
    """Complete once, with a row lock so simultaneous taps have one winner."""
    locked = (
        await db.execute(
            select(Task).where(Task.id == task.id).with_for_update()
        )
    ).scalar_one_or_none()
    if locked is None:
        setattr(task, "_completion_won", False)
        return task
    if locked.status != TASK_OPEN:
        log.info("task_completion_duplicate", code=locked.code, status=locked.status, by=by)
        setattr(task, "_completion_won", False)
        setattr(locked, "_completion_won", False)
        await db.rollback()
        return locked

    setattr(task, "_completion_won", True)
    setattr(locked, "_completion_won", True)
    locked.status = TASK_DONE
    locked.completed_at = datetime.now(timezone.utc)
    if reply:
        locked.reply = reply[:1000]
    db.add(locked)
    await db.commit()
    await audit.record(
        actor_role="staff" if by != "dashboard" else "admin", actor=by,
        action="task_completed", args={"code": locked.code}, result=(reply or "done")[:150],
    )
    log.info("task_completed", code=locked.code, by=by)
    _announce(locked, "done", by=by)
    if advance_order:
        from app.services import ops_agent
        await ops_agent.on_task_done(db, locked, by)
    return locked


async def completion_message(db: AsyncSession, task: Task, *, by: str) -> str:
    """Return a safe response when a team member taps an already-finished task."""
    if task.status == TASK_DONE:
        return f"ℹ️ *{task.code}* already complete ho chuka hai. Pehla update accept hua tha; aapka duplicate update ignore kiya gaya."
    return ""

async def cancel_task(db: AsyncSession, task: Task, *, by: str = "dashboard") -> Task:
    task.status = TASK_CANCELLED
    task.completed_at = datetime.now(timezone.utc)
    db.add(task)
    await db.commit()
    await audit.record(
        actor_role="admin", actor=by, action="task_cancelled",
        args={"code": task.code}, result="cancelled",
    )
    _announce(task, "cancelled", by=by)
    return task


async def cancel_open_tasks_for_order(db: AsyncSession, order_id, *, by: str = "agent") -> int:
    """Us order ke khule kaam rok do. Returns kitne roke.

    Order hold/cancel ho jaye to uski delivery ka peechha karte rehna sirf
    Ajit ko pareshan karta hai — aur owner ko jhoothe reminder deta hai.
    """
    rows = (
        await db.execute(
            select(Task).where(Task.order_id == order_id, Task.status == TASK_OPEN)
        )
    ).scalars().all()
    for t in rows:
        await cancel_task(db, t, by=by)
    return len(rows)


async def get_by_code(db: AsyncSession, code: str) -> Task | None:
    code = (code or "").strip().upper()
    if not code.startswith("T-"):
        code = f"T-{code.lstrip('T-')}"
    return (
        await db.execute(
            select(Task)
            .where(Task.code == code)
            .order_by(Task.created_at.desc(), Task.id.desc())
            .limit(1)
        )
    ).scalars().first()


async def open_tasks_for_staff(db: AsyncSession, staff_id) -> list[Task]:
    """Open queue visible to the staff member's shared operational team."""
    cond = await _staff_open_task_filter(db, staff_id)
    if cond is None:
        return []
    return list(
        (
            await db.execute(
                select(Task)
                .where(cond, Task.status == TASK_OPEN)
                .order_by(Task.last_ping_at.desc().nullslast(), Task.created_at)
            )
        )
        .scalars()
        .all()
    )


async def note_reply(
    db: AsyncSession, staff_id, text: str, *, by: str | None = None,
    task_code: str | None = None,
) -> Task | None:
    """Store a reply only against an unambiguous task.

    A staff member can have several open jobs. Never guess that a free-text
    reply belongs to the most recently pinged task; menu replies should pass
    their embedded task code, while unreferenced text is recorded only when
    exactly one open task is available.
    """
    tasks = await open_tasks_for_staff(db, staff_id)
    if task_code:
        task = await get_by_code(db, task_code)
        if task is None or task.status != TASK_OPEN:
            return None
        if staff_id is not None:
            staff = await db.get(Staff, staff_id)
            if staff is None or not await staff_can_access_task(db, task, staff):
                return None
    else:
        if len(tasks) != 1:
            if len(tasks) > 1:
                log.info("task_reply_ambiguous_not_attached", staff_id=str(staff_id), open_tasks=len(tasks))
            return None
        task = tasks[0]
    author = by or "staff"
    task.reply = text[:1000]
    db.add(task)
    await db.commit()
    await audit.record(
        actor_role="staff", actor=author,
        action="task_reply", args={"code": task.code}, result=text[:150],
    )
    _announce(task, "reply", by=author)
    return task


def _task_ping_gap_hours(task: Task, order: Order | None, now_ist: datetime) -> int:
    """Deadline-aware cadence: as delivery approaches, reminders get tighter."""
    if order is not None and order.expected_delivery is not None:
        days_left = (order.expected_delivery - now_ist.date()).days
        if days_left <= 0:
            return 1
        if days_left == 1:
            return 1
        if days_left == 2:
            return 2
        if days_left == 3:
            return 4
    return URGENT_PING_AFTER_HOURS if task.urgent else PING_AFTER_HOURS


async def _close_obsolete_job_task(db: AsyncSession, task: Task, order: Order | None) -> bool:
    """Stop reminders for job tasks whose order has already moved past them.

    A stale OPEN task can survive when the order status is advanced by another
    path (dashboard, agent, or a duplicate staff action). The follow-up worker
    must trust the order lifecycle as well as the task row, otherwise a
    delivery can be completed in the DB while an old task keeps getting
    reminders.
    """
    if task.kind not in JOB_KINDS:
        return False

    # A pickup/delivery task without an order is stale/orphaned. It cannot be
    # actioned safely because there is no customer or order lifecycle to
    # advance. Close it instead of sending another generic reminder.
    if order is None:
        # A customer-only operational task is valid: it is still linked to
        # the customer and can be followed up normally. Only a task with
        # neither an order nor a customer is a true legacy orphan.
        if task.customer_id is not None:
            return False
        task.status = TASK_CANCELLED
        task.completed_at = datetime.now(timezone.utc)
        task.reply = task.reply or "closed: job task has no linked order/customer"
        db.add(task)
        await db.commit()
        _announce(task, "cancelled", by="orphan_job_guard")
        return True

    from app.models import OrderStatus

    if order.status in (OrderStatus.CANCELLED, OrderStatus.ON_HOLD):
        task.status = TASK_CANCELLED
        task.completed_at = datetime.now(timezone.utc)
        db.add(task)
        await db.commit()
        _announce(task, "cancelled", by="order_status_guard")
        return True

    if task.kind == "pickup" and order.status in {
        OrderStatus.PICKED_UP,
        OrderStatus.IN_WASH,
        OrderStatus.IN_DRY,
        OrderStatus.IN_IRON,
        OrderStatus.READY,
        OrderStatus.OUT_FOR_DELIVERY,
        OrderStatus.DELIVERED,
    }:
        task.status = TASK_DONE
        task.completed_at = datetime.now(timezone.utc)
        task.reply = task.reply or "closed by order lifecycle"
        db.add(task)
        await db.commit()
        _announce(task, "done", by="order_status_guard")
        return True

    if task.kind == "delivery" and order.status is OrderStatus.DELIVERED:
        task.status = TASK_DONE
        task.completed_at = datetime.now(timezone.utc)
        task.reply = task.reply or "delivery completed via order lifecycle"
        db.add(task)
        await db.commit()
        _announce(task, "done", by="order_status_guard")
        return True

    return False


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
            staff = await db.get(Staff, task.assigned_staff_id)
            if staff is None or not staff.is_active:
                continue
            order = await db.get(Order, task.order_id) if task.order_id else None
            if await _close_obsolete_job_task(db, task, order):
                continue
            # ping_count is intentionally a DAILY follow-up count.
            # We never send more than three reminders to one staff member for
            # one open task in a calendar day. At the first follow-up of a new
            # day the counter starts again; escalated_at stays permanent so
            # the manager is not spammed every morning.
            if task.last_ping_at is not None:
                last_ping_ist = task.last_ping_at.astimezone(IST)
                if last_ping_ist.date() != now_ist.date():
                    task.ping_count = 0
                    db.add(task)
                    await db.commit()

            gap_hours = _task_ping_gap_hours(task, order, now_ist)
            since = task.last_ping_at or task.created_at
            if (now - since) < timedelta(hours=gap_hours):
                continue

            # After three staff reminders, tell the manager instead of sending
            # a fourth staff message. This escalation is separate from the
            # three-per-day staff limit.
            if task.ping_count >= ESCALATE_AFTER_PINGS and task.escalated_at is None:
                waited = int((now - task.created_at).total_seconds() // 3600)
                try:
                    from app.services import team
                    await send_message(
                        db, to_phone=await team.primary_admin_phone(db),
                        text=(
                            f"🚨 *{staff.name} — {task.code} ka response pending*\n"
                            f"━━━━━━━━━━━━━━\n"
                            f"⏱️ {waited} ghante se jawab nahi aaya.\n"
                            f"📌 *Task:* {task.title}\n"
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

            if task.ping_count >= MAX_FOLLOWUPS_PER_DAY:
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
