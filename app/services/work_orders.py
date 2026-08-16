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
from app.services.whatsapp import (
    MAX_LIST_ROWS,
    Button,
    ListRow,
    SendError,
    WindowClosedError,
    send_message,
)

log = structlog.get_logger()


# Staff ko jo bhi kaam ka message jaye — naya order, aaj-delivery ka
# reminder, subah ka standup, task ki yaad-dahani — uspar HAMESHA yahi teen
# jawab hone chahiye. Ajit aur Ravi ka poora din inhi teen tap par chalta
# hai; button ke id mein order number / task code hota hai, isliye 5-6 kaam
# khule hone par bhi "kaunsa" kabhi galat nahi hota (aur AI ko bhi kuch
# samajhna nahi padta — id seedha bata deti hai).
MAX_BUTTON_TITLE = 20   # WhatsApp ki hadd


async def _labels(db: AsyncSession) -> dict[str, str]:
    """Is shop ke apne button-shabd. Har shop alag rakh sakti hai."""
    out = {}
    for slot, key in (
        ("done", "agent_btn_done"),
        ("later", "agent_btn_later"),
        ("problem", "agent_btn_problem"),
    ):
        try:
            label = (await app_settings.get(db, key) or "").strip()
        except Exception:
            label = ""
        # Khali chhod diya ya bahut lamba likh diya to bhi message jana
        # chahiye — Meta 20 se lamba title reject karta hai.
        out[slot] = (label or app_settings.DEFAULTS[key])[:MAX_BUTTON_TITLE]
    return out


async def list_button_label(db: AsyncSession) -> str:
    try:
        label = (await app_settings.get(db, "agent_list_button") or "").strip()
    except Exception:
        label = ""
    return (label or app_settings.DEFAULTS["agent_list_button"])[:MAX_BUTTON_TITLE]


async def order_buttons(db: AsyncSession, order_number: str) -> list[Button]:
    lb = await _labels(db)
    return [
        Button(f"ord:{order_number}:done", lb["done"]),
        Button(f"ord:{order_number}:later", lb["later"]),
        Button(f"ord:{order_number}:problem", lb["problem"]),
    ]


async def task_buttons(db: AsyncSession, code: str) -> list[Button]:
    lb = await _labels(db)
    return [
        Button(f"task:{code}:done", lb["done"]),
        Button(f"task:{code}:later", lb["later"]),
        Button(f"task:{code}:problem", lb["problem"]),
    ]


async def work_rows(db: AsyncSession, orders, tasks=()) -> list[ListRow]:
    """Kaam ki tappable list — buttons sirf 3 ho sakte hain, list 10.

    Meta ki hadd: title 24 akshar, description 72 — isliye yahin kaat dete
    hain, warna Meta poora message reject kar deta aur staff tak kuch nahi
    pahunchta.
    """
    rows: list[ListRow] = []
    for o in orders:
        if len(rows) >= MAX_LIST_ROWS:
            break
        cust = await db.get(Customer, o.customer_id)
        who = (cust.name or cust.phone) if cust else "?"
        desc = f"{who} — {items_summary(o)}"
        if o.priority == "urgent":
            desc = "🔴 " + desc
        rows.append(
            ListRow(id=f"pick:o:{o.order_number}", title=o.order_number[:24],
                    description=desc[:72])
        )
    for t in tasks:
        if len(rows) >= MAX_LIST_ROWS:
            break
        rows.append(
            ListRow(id=f"pick:t:{t.code}", title=f"{t.code} · {t.title}"[:24],
                    description=(("🔴 " if t.urgent else "") + t.title)[:72])
        )
    return rows


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
        # Buttons ke saath — Ajit/Ravi ko likhna na pade. Free-form text
        # ka wahi rasta hai, bas teen tap upar se.
        await send_message(
            db, to_phone=staff.phone, text=text,
            buttons=await order_buttons(db, order.order_number),
        )
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
