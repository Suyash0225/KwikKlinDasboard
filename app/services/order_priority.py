"""Deterministic order-priority workflow.

No LLM is used here. When an active order reaches the configured urgent
window before its promised delivery date, mark it urgent and escalate the
existing open work to the manager/washer. This keeps operational scheduling
cheap and predictable; AI remains responsible for natural-language/customer
communication.
"""

from datetime import date, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderStatus, Task, Staff
from app.models.task import TASK_OPEN
from app.services import app_settings
from app.services.whatsapp import SendError, send_message
from app.services.tenant_context import manager_phone

log = structlog.get_logger()

_TERMINAL = {OrderStatus.DELIVERED, OrderStatus.CANCELLED}


async def refresh_urgent_orders(
    db: AsyncSession, *, today: date | None = None
) -> int:
    """Promote orders that are within the urgent delivery window.

    Default: one working/calendar day before the promised date, controlled by
    the existing `urgent_delivery_days` setting. The promise itself is never
    changed. This only changes priority and existing open tasks.
    """
    today = today or date.today()
    try:
        days = int(await app_settings.get(db, "urgent_delivery_days") or 1)
    except (TypeError, ValueError):
        days = 1
    days = max(0, min(days, 14))

    cutoff = today + timedelta(days=days)
    rows = (
        await db.execute(
            select(Order)
            .where(
                Order.expected_delivery.is_not(None),
                Order.expected_delivery <= cutoff,
                Order.status.notin_(_TERMINAL),
                Order.priority != "urgent",
            )
            .order_by(Order.expected_delivery.asc())
        )
    ).scalars().all()

    if not rows:
        return 0

    promoted: list[Order] = []
    for order in rows:
        order.priority = "urgent"
        promoted.append(order)

        # Existing open work becomes urgent immediately. If no task exists,
        # ops_agent.plan_due_wash_tasks will create the correct task later in
        # the same scheduler tick using the new order priority.
        tasks = (
            await db.execute(
                select(Task).where(
                    Task.order_id == order.id,
                    Task.status == TASK_OPEN,
                )
            )
        ).scalars().all()
        for task in tasks:
            task.urgent = True

    await db.commit()

    # Operational alerts are deterministic/template-like notifications:
    # do not spend an LLM call just to say an order is urgent.
    for order in promoted:
        remaining = (order.expected_delivery - today).days
        msg = (
            f"🔴 URGENT ORDER\n"
            f"Order: {order.order_number}\n"
            f"Delivery: {order.expected_delivery.strftime('%d %b %Y')} "
            f"({remaining} day(s) left)\n"
            f"Status: {order.status.name.replace('_', ' ').title()}\n"
            f"Please prioritize this order."
        )

        try:
            phone = manager_phone()
            if phone:
                await send_message(db, to_phone=phone, text=msg, sent_by="system")
        except Exception:
            log.exception("urgent_manager_alert_failed", order=order.order_number)

        if order.assigned_washer_id:
            try:
                washer = await db.get(Staff, order.assigned_washer_id)
                if washer and washer.is_active and washer.phone:
                    await send_message(
                        db,
                        to_phone=washer.phone,
                        text=msg,
                        sent_by="system",
                    )
            except Exception:
                log.exception(
                    "urgent_washer_alert_failed",
                    order=order.order_number,
                )

    log.info("orders_promoted_urgent", count=len(promoted))
    return len(promoted)
