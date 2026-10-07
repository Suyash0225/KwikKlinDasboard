"""Ops agent — order banate hi kaam khud baantna aur sambhalna (IMP_007).

Bill kahin se bhi bane (dashboard, staff panel, WhatsApp bill agent, grahak
ka pickup chat), create_order ke andar yahi agent chalta hai:

  1. Washerman chunta hai  -> order.assigned_washer_id + "wash" task
  2. Pickup ho to delivery boy chunta hai -> assigned_delivery_id + pickup
     task (wahi "kab tak?" wali baat-cheet, tasks.py)
  3. Status aage badhe to purana kaam band, agla kaam khud:
       PICKED_UP -> pickup task done
       IN_DRY    -> wash task done, drying task bane
       IN_IRON   -> drying task done, ironing task bane
       READY     -> ironing task done, delivery task bane
       DELIVERED -> delivery task done
       CANCELLED / ON_HOLD -> saare khule kaam band
  4. Washerman panel par stage task "done" kare -> order agle processing stage par
  5. Har ghante: jin kaamon ka aadmi hata diya gaya/inactive hai, unhe
     doosre ko de do (rebalance)

Chunna: us role ke active staff mein jiske paas SABSE KAM khule kaam hain.
Barabar ho to Settings ka default aadmi, phir naam. Order par pehle se koi
lagaya hai (owner ne "assign" kiya) to wahi. Koi LLM nahi — kaam baantna
har baar ek jaisa, samjhaane layak hona chahiye.

Owner Settings -> "Agent assigns work automatically" band kar sakta hai
(`agent_auto_assign`). Band ho to purana tareeka: sirf default aadmi ko
WhatsApp work order.

Kabhi raise nahi karta: kaam baantne ki galti se bill nahi girna chahiye.
"""

from datetime import date, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderStatus, Staff, StaffRole, Task
from app.models.task import TASK_OPEN
from app.services import audit

log = structlog.get_logger()

ROLES = {
    "WASHER": (StaffRole.WASHER, StaffRole.SUPERVISOR),
    "DELIVERY": (StaffRole.DELIVERY,),
}
_DEFAULT_SETTING = {"WASHER": "default_washer_phone", "DELIVERY": "default_delivery_phone"}
_ORDER_FIELD = {"WASHER": "assigned_washer_id", "DELIVERY": "assigned_delivery_id"}
# Ye statuses aane par ye kaam poore maane jaate hain
_DONE_ON = {
    OrderStatus.PICKED_UP: ("pickup",),
    OrderStatus.IN_DRY: ("wash",),
    OrderStatus.IN_IRON: ("wash", "dry"),
    OrderStatus.READY: ("pickup", "wash", "dry", "iron"),
    OrderStatus.OUT_FOR_DELIVERY: ("pickup", "wash", "dry", "iron"),
    OrderStatus.DELIVERED: ("pickup", "wash", "dry", "iron", "delivery"),
}

_STAGE_TASKS = {
    OrderStatus.IN_DRY: {
        "kind": "dry",
        "title": "{who} — drying: {items} | delivery {delivery}",
    },
    OrderStatus.IN_IRON: {
        "kind": "iron",
        "title": "{who} — ironing: {items} | delivery {delivery}",
    },
}
_WASHABLE = (OrderStatus.RECEIVED, OrderStatus.PICKED_UP, OrderStatus.IN_WASH,
             OrderStatus.IN_DRY, OrderStatus.IN_IRON)
# Only these stages can legitimately be waiting for a new wash task.
_WASH_PLANNABLE = (OrderStatus.RECEIVED, OrderStatus.PICKED_UP, OrderStatus.IN_WASH)


async def enabled(db: AsyncSession) -> bool:
    from app.services import app_settings

    try:
        return bool(await app_settings.get(db, "agent_auto_assign"))
    except Exception:
        return True


async def open_load(db: AsyncSession) -> dict:
    """staff_id -> kitne khule kaam."""
    rows = (
        await db.execute(
            select(Task.assigned_staff_id, func.count())
            .where(Task.status == TASK_OPEN, Task.assigned_staff_id.isnot(None))
            .group_by(Task.assigned_staff_id)
        )
    ).all()
    return {sid: n for sid, n in rows}


async def pick_staff(db: AsyncSession, role: str, order: Order | None = None) -> Staff | None:
    """Is kaam ke liye kaun — order par laga aadmi, warna sabse khaali."""
    from app.services import app_settings

    roles = ROLES[role]
    if order is not None:
        sid = getattr(order, _ORDER_FIELD[role])
        if sid is not None:
            st = await db.get(Staff, sid)
            if st is not None and st.is_active:
                return st
    staff = (
        await db.execute(select(Staff).where(Staff.is_active.is_(True), Staff.role.in_(roles)))
    ).scalars().all()
    if not staff:
        return None
    try:
        default_phone = (await app_settings.get(db, _DEFAULT_SETTING[role]) or "").strip()
    except Exception:
        default_phone = ""
    load = await open_load(db)
    return min(staff, key=lambda s: (load.get(s.id, 0), s.phone != default_phone, s.name.casefold()))


async def _open_tasks(db: AsyncSession, order_id, kinds) -> list[Task]:
    return list(
        (
            await db.execute(
                select(Task).where(
                    Task.order_id == order_id, Task.status == TASK_OPEN, Task.kind.in_(kinds)
                )
            )
        ).scalars().all()
    )


async def on_order_created(db: AsyncSession, order: Order) -> dict:
    """Naye bill par kaam baanto. Lautata hai kisko kya diya (log/test ke liye)."""
    out: dict = {"wash": None, "pickup": None}
    num = order.order_number   # rollback ke baad object expire ho jaata hai
    try:
        if not await enabled(db):
            return out
        from app.models import Customer
        from app.services import tasks as task_service
        from app.services.work_orders import items_summary

        washer = await pick_staff(db, "WASHER", order)
        if washer is not None:
            if order.assigned_washer_id is None:
                order.assigned_washer_id = washer.id
                db.add(order)
                await db.commit()
            if not await _open_tasks(db, order.id, ("wash",)):
                customer = await db.get(Customer, order.customer_id)
                who = (customer.name or customer.phone) if customer else "Customer"
                # Washing is planned from the delivery promise, not from
                # order creation. If delivery is >3 days away, don't ping the
                # washer yet — the scheduler will create the task in the
                # preparation window. This keeps WhatsApp quiet and avoids
                # unnecessary staff messages.
                if (
                    order.status is OrderStatus.PICKED_UP
                    or (
                        order.status in (OrderStatus.RECEIVED, OrderStatus.IN_WASH)
                        and (
                            order.expected_delivery is None
                            or order.expected_delivery <= date.today() + timedelta(days=3)
                        )
                    )
                ):
                    task = await task_service.create_task(
                        db,
                        title=f"{who} — wash & iron: {items_summary(order)[:160]}",
                        staff=washer, order=order, urgent=order.priority == "urgent",
                        created_by="agent", kind="wash",
                        notify=True,
                    )
                    out["wash"] = {"code": task.code, "staff": washer.name}

        if order.status is OrderStatus.PICKUP_ASSIGNED:
            boy = await pick_staff(db, "DELIVERY", order)
            if boy is not None:
                if order.assigned_delivery_id is None:
                    order.assigned_delivery_id = boy.id
                    db.add(order)
                    await db.commit()
                task = await task_service.create_pickup_task(db, order)
                if task is not None:
                    out["pickup"] = {"code": task.code, "staff": boy.name}
        if out["wash"] or out["pickup"]:
            await audit.record(
                actor_role="system", actor="ops-agent", action="work_auto_assigned",
                args={"order": order.order_number, **{k: v for k, v in out.items() if v}},
            )
    except Exception:
        # Session ko tuta hua mat chhodo — bill ka baaki kaam (grahak ko
        # message, payment) isi session par chalta hai
        await db.rollback()
        log.exception("ops_agent_order_created_failed", order=num)
    return out


async def plan_due_wash_tasks(db: AsyncSession, *, today=None) -> int:
    """Create/notify wash tasks only when delivery is within 3 days.

    Deterministic DB workflow: no LLM call. If the order is already READY,
    OUT_FOR_DELIVERY, DELIVERED, CANCELLED or ON_HOLD, nothing is sent.
    Idempotency comes from the existing open-task check.
    """
    from datetime import date, timedelta
    from app.models import Customer
    from app.services import tasks as task_service
    from app.services.work_orders import items_summary

    today = today or date.today()
    cutoff = today + timedelta(days=3)
    rows = (
        await db.execute(
            select(Order).where(
                Order.expected_delivery.isnot(None),
                Order.expected_delivery <= cutoff,
                Order.expected_delivery >= today,
                Order.status.in_(_WASH_PLANNABLE),
            )
        )
    ).scalars().all()

    created = 0
    for order in rows:
        if await _open_tasks(db, order.id, ("wash",)):
            continue
        washer = await pick_staff(db, "WASHER", order)
        if washer is None:
            continue
        if order.assigned_washer_id is None:
            order.assigned_washer_id = washer.id
            db.add(order)
            await db.commit()
        customer = await db.get(Customer, order.customer_id)
        who = (customer.name or customer.phone) if customer else "Customer"
        task = await task_service.create_task(
            db,
            title=f"{who} — wash & iron: {items_summary(order)[:160]} | delivery {order.expected_delivery.strftime('%d %b')}",
            staff=washer,
            order=order,
            urgent=order.priority == "urgent" or order.expected_delivery <= today + timedelta(days=1),
            created_by="ops-agent",
            kind="wash",
            notify=True,
        )
        created += 1
        await audit.record(
            actor_role="system",
            actor="ops-agent",
            action="wash_task_planned",
            args={"order": order.order_number, "staff": washer.name, "days_to_delivery": (order.expected_delivery - today).days},
            result=task.code,
        )
    return created


async def _ensure_stage_task(db: AsyncSession, order: Order, status: OrderStatus) -> Task | None:
    """Create the next processing task exactly once for the assigned washer."""
    cfg = _STAGE_TASKS.get(status)
    if cfg is None or not await enabled(db):
        return None

    from app.models import Customer
    from app.services import tasks as task_service
    from app.services.work_orders import items_summary

    if await _open_tasks(db, order.id, (cfg["kind"],)):
        return None

    washer = await pick_staff(db, "WASHER", order)
    if washer is None:
        return None
    if order.assigned_washer_id is None:
        order.assigned_washer_id = washer.id
        db.add(order)
        await db.commit()

    customer = await db.get(Customer, order.customer_id)
    who = (customer.name or customer.phone) if customer else "Customer"
    delivery = order.expected_delivery.strftime("%d %b") if order.expected_delivery else "date confirm"
    urgent = bool(
        order.priority == "urgent"
        or (
            order.expected_delivery is not None
            and order.expected_delivery <= date.today() + timedelta(days=1)
        )
    )
    return await task_service.create_task(
        db,
        title=cfg["title"].format(
            who=who, items=items_summary(order)[:160], delivery=delivery
        ),
        staff=washer,
        order=order,
        urgent=urgent,
        created_by="ops-agent",
        kind=cfg["kind"],
        notify=True,
    )


async def on_status_change(db: AsyncSession, order: Order, new_status: OrderStatus, by: str) -> None:
    """Order aage badha — jo kaam ho chuka use band karo, ruka to sab band."""
    num = order.order_number
    try:
        from app.services import tasks as task_service

        if new_status in (OrderStatus.CANCELLED, OrderStatus.ON_HOLD):
            await task_service.cancel_open_tasks_for_order(db, order.id, by="ops-agent")
            return
        kinds = _DONE_ON.get(new_status)
        if not kinds:
            return
        for t in await _open_tasks(db, order.id, kinds):
            await task_service.complete_task(
                db, t, reply=f"order {new_status.name.lower().replace('_', ' ')}", by=by or "ops-agent",
                advance_order=False,
            )

        # Pickup is the gate for washing work on home-pickup orders.
        # The washer must never receive an order before the delivery boy has
        # actually collected the clothes. Once PICKED_UP is committed, create
        # the wash task immediately (without the 3-day planning delay).
        if new_status is OrderStatus.PICKED_UP and await enabled(db):
            if not await _open_tasks(db, order.id, ("wash",)):
                from app.models import Customer
                from app.services import tasks as task_service
                from app.services.work_orders import items_summary

                washer = await pick_staff(db, "WASHER", order)
                if washer is not None:
                    if order.assigned_washer_id is None:
                        order.assigned_washer_id = washer.id
                        db.add(order)
                        await db.commit()
                    customer = await db.get(Customer, order.customer_id)
                    who = (customer.name or customer.phone) if customer else "Customer"
                    await task_service.create_task(
                        db,
                        title=f"{who} — wash & iron: {items_summary(order)[:160]}",
                        staff=washer,
                        order=order,
                        urgent=order.priority == "urgent",
                        created_by="ops-agent",
                        kind="wash",
                        notify=True,
                    )

        # Manual/status-driven moves must also create the next processing task.
        if new_status in _STAGE_TASKS:
            await _ensure_stage_task(db, order, new_status)

        # READY is the handoff point: processing is complete, so the next
        # open job must belong to the delivery team. create_delivery_task()
        # is idempotent and also writes order.assigned_delivery_id, keeping
        # the DB assignment and the staff task in sync.
        if new_status is OrderStatus.READY and await enabled(db):
            await task_service.create_delivery_task(db, order)
    except Exception:
        await db.rollback()
        log.exception("ops_agent_status_hook_failed", order=num)


async def on_task_done(db: AsyncSession, task: Task, by: str) -> None:
    """A processing task is a real workflow step, not a shortcut to READY."""
    targets = {
        "wash": (OrderStatus.IN_WASH, OrderStatus.IN_DRY),
        "dry": (OrderStatus.IN_DRY, OrderStatus.IN_IRON),
        "iron": (OrderStatus.IN_IRON, OrderStatus.READY),
    }
    if task.kind not in targets or task.order_id is None:
        return
    expected, target = targets[task.kind]
    try:
        from app.services import order_service

        order = await db.get(Order, task.order_id)
        if order is None:
            return

        # A drop-off order can start at RECEIVED; record IN_WASH before
        # moving the completed wash into drying. Pickup orders reach
        # IN_WASH after the pickup/status transition.
        if task.kind == "wash" and order.status in (OrderStatus.RECEIVED, OrderStatus.PICKED_UP):
            await order_service.update_status(
                db, order, OrderStatus.IN_WASH, changed_by=f"staff:{by}"
            )
            order = await db.get(Order, task.order_id)

        if order is not None and order.status is expected:
            await order_service.update_status(
                db, order, target, changed_by=f"staff:{by}"
            )
    except Exception:
        log.exception("ops_agent_stage_done_failed", code=task.code, kind=task.kind)


async def rebalance(db: AsyncSession) -> int:
    """Agent ke khule kaam jinka aadmi ab active nahi — doosre ko do.
    Lautata hai kitne kaam badle."""
    moved = 0
    try:
        if not await enabled(db):
            return 0
        from app.services import tasks as task_service

        rows = (
            await db.execute(
                select(Task, Staff)
                .join(Staff, Staff.id == Task.assigned_staff_id)
                .where(Task.status == TASK_OPEN, Task.kind.in_(("wash", "pickup", "delivery")),
                       Staff.is_active.is_(False))
            )
        ).all()
        for task, old in rows:
            role = "WASHER" if task.kind in ("wash", "dry", "iron") else "DELIVERY"
            order = await db.get(Order, task.order_id) if task.order_id else None
            if order is not None and getattr(order, _ORDER_FIELD[role]) == old.id:
                setattr(order, _ORDER_FIELD[role], None)
            new = await pick_staff(db, role, order)
            if new is None or new.id == old.id:
                continue
            task.assigned_staff_id = new.id
            task.ping_count = 0
            task.escalated_at = None
            if order is not None:
                setattr(order, _ORDER_FIELD[role], new.id)
                db.add(order)
            db.add(task)
            await db.commit()
            await task_service.send_task_to(db, task, new)
            await audit.record(
                actor_role="system", actor="ops-agent", action="task_reassigned",
                args={"code": task.code, "from": old.name, "to": new.name},
            )
            moved += 1
    except Exception:
        log.exception("ops_agent_rebalance_failed")
    return moved
