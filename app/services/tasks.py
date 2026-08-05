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
