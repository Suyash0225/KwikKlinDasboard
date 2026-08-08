"""Staff work-order notifications: specific, unambiguous instructions.

Rule (owner's spec): never a vague forward. Every staff instruction names
the order, the customer, the items and the deadline. Free-form first,
kk_staff_alert template when the 24h window is closed, log-only last.
"""

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer, Order, Staff
from app.services import app_settings
from app.services.messages import get_message
from app.services.whatsapp import SendError, WindowClosedError, send_message

log = structlog.get_logger()


def items_summary(order: Order) -> str:
    """'3 x Shirt, 2 x Pant' from the items JSON."""
    parts = []
    for it in order.items or []:
        qty = it.get("qty", 1)
        qty = int(qty) if float(qty).is_integer() else qty
        parts.append(f"{qty} x {it.get('type') or it.get('garment') or it.get('service', '?')}")
    return ", ".join(parts) or "items dashboard par"


async def resolve_worker(db: AsyncSession, order: Order, role: str = "WASHER") -> Staff | None:
    """Who this instruction belongs to.

    role="DELIVERY" -> the assigned delivery boy, else the shop's delivery
    person. Pickup and delivery reminders used to land on the WASHER, so the
    boy who actually goes out never heard them.
    """
    if role == "DELIVERY":
        if order.assigned_delivery_id:
            st = await db.get(Staff, order.assigned_delivery_id)
            if st:
                return st
        from app.services import team

        return await team.delivery_staff(db)
    if order.assigned_washer_id:
        st = await db.get(Staff, order.assigned_washer_id)
        if st:
            return st
    default_phone = await app_settings.get(db, "default_washer_phone")
    if default_phone:
        return (
            await db.execute(select(Staff).where(Staff.phone == default_phone))
        ).scalar_one_or_none()
    return None


async def send_work_order(
    db: AsyncSession, order: Order, *, headline: str, extra: str = "", role: str = "WASHER"
) -> str:
    """Send a complete work order to the responsible staff member.

    Returns 'sent' | 'sent_template' | 'no_staff' | 'failed' — caller
    reports it honestly to the admin. Never raises.
    """
    staff = await resolve_worker(db, order, role)
    if staff is None:
        log.warning("work_order_no_staff", order_number=order.order_number)
        return "no_staff"

    customer = await db.get(Customer, order.customer_id)
    text = get_message(
        "work_order",
        headline=headline,
        order_number=order.order_number,
        customer_name=(customer.name if customer and customer.name else customer.phone if customer else "?"),
        items=items_summary(order),
        delivery=order.expected_delivery.strftime("%d %b %Y") if order.expected_delivery else "jaldi batayenge",
        priority="🔴 URGENT" if order.priority == "urgent" else "normal",
        extra=extra or "-",
    )
    try:
        await send_message(db, to_phone=staff.phone, text=text)
        return "sent"
    except WindowClosedError:
        try:
            await send_message(
                db,
                to_phone=staff.phone,
                template_name="kk_staff_alert",
                template_params=[" ".join(text.split())[:600]],
            )
            return "sent_template"
        except SendError:
            log.warning("work_order_not_sent", order_number=order.order_number, to=staff.phone)
            return "failed"
    except SendError:
        log.warning("work_order_send_failed", order_number=order.order_number, to=staff.phone)
        return "failed"
