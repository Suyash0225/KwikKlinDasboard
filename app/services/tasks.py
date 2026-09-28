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
QUIET_START, QUIET_END = settings.QUIET_HOURS_START, settings.QUIET_HOURS_END


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
    urgent: bool = False,
    created_by: str = "owner",
    notify: bool = True,
    kind: str = "general",
) -> Task:
    """Record the task and tell the assignee. Returns the saved Task."""
    task = Task(
        code=await _next_code(db),
        title=title.strip(),
        assigned_staff_id=staff.id if staff else None,
        order_id=order.id if order else None,
        urgent=urgent,
        created_by=created_by[:40],
        kind=kind,
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
        complete_line = f"Kaam complete hone ke baad bas reply karein: done {task.code}"
        update_line = f"Kaam ka update bhej dein, ya complete hone par reply karein: done {task.code}"
    else:
        head = "🔴 URGENT" if task.urgent else "📋 TASK ASSIGNMENT"
        if task.kind == "wash": head = "🧼 WASHING TASK"
        elif task.kind == "dry": head = "💨 DRYING TASK"
        elif task.kind == "iron": head = "👔 IRONING TASK"
        elif task.kind == "pickup": head = "🧺 PICKUP TASK"
        elif task.kind == "delivery": head = "🚚 DELIVERY TASK"
        complete_line = f"After completing the task, reply: done {task.code}"
        update_line = f"Send an update, or reply when complete: done {task.code}"
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
    # Tap = zero typing. Button id mein task ka CODE hai, isliye 5-6 kaam
    # ek saath pending hon tab bhi galat task kabhi band nahi hota. Likh kar
    # jawab dena ("done T-11", ya poori baat) waise hi chalta rahega —
    # button sirf sabse aam jawab ka shortcut hai.
    try:
        # Assignment notification is a critical WhatsApp message. In WAHA
        # mode use plain text so delivery does not depend on interactive-list
        # support. The task code is still included for "done T-123" replies.
        # First assignment is a clean task card. Add the action menu only
        # on reminders/when the task is getting close to needing an update.
        if first:
            await send_message(
                db, to_phone=staff.phone, text=body, sent_by="bot"
            )
        else:
            from app.services.work_orders import task_buttons
            buttons = await task_buttons(db, task.code)
            await send_message(
                db, to_phone=staff.phone, text=body, buttons=buttons, sent_by="bot"
            )
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
        "done_reply": "👍 Thank you. Pickup marked as done.",
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
        "done_reply": "👍 Delivery marked as done. Customer updated.",
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
            + (f"Pata: {addr}\n" if addr else "")
            + f"\n{cfg['ask']}"
        )
        delivered = "no"
        try:
            await send_message(db, to_phone=staff.phone, text=ask, sent_by="bot")
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
            "yes": "Unse samay pooch liya hai, pata chalte hi bata dunga.",
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
        await send_message(
            db, to_phone=staff.phone,
            text=f"📋 {cfg['word'].upper()} TASK [{task.code}]\\n{question}",
            buttons=[
                Button(f"job_yes:{task.code}", done_title),
                Button(f"job_no:{task.code}", pending_title),
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
    db: AsyncSession, task: Task, *, reply: str | None = None, by: str = "staff",
    advance_order: bool = True,
) -> Task:
    """advance_order: wash task done = order READY (ops agent). False jab
    order khud aage badh chuka ho aur ye band karna sirf safai hai."""
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
    _announce(task, "done", by=by)
    if advance_order:
        from app.services import ops_agent

        await ops_agent.on_task_done(db, task, by)
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
    # Staff ne kuch kaha — "nahi ho paega", "ho gaya", "time lagega". Ye
    # wahi baat hai jiske liye owner baar-baar refresh karta tha.
    _announce(task, "reply", by="staff")
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
            gap_hours = _task_ping_gap_hours(task, order, now_ist)
            since = task.last_ping_at or task.created_at
            if (now - since) < timedelta(hours=gap_hours):
                continue

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

            if await _send_to_assignee(db, task, staff, first=False):
                sends += 1
            task.ping_count += 1
            task.last_ping_at = now
            db.add(task)
            await db.commit()

    if sends:
        log.info("task_followups_sent", count=sends)
    return sends
