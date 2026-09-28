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
from app.services.urgent import KIND as URGENT_KIND
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
    """Minimal action labels following the tenant communication language."""
    try:
        lang = str(await app_settings.get(db, "communication_language") or "en").lower()
    except Exception:
        lang = "en"
    if lang == "hi":
        return {"done": "✅ Ho gaya", "later": "⏳ Der lagegi", "problem": "⚠️ Dikkat", "list": "Kaam chuniye"}
    return {"done": "✅ Done", "later": "⏳ Need more time", "problem": "⚠️ Problem", "list": "Select task"}


async def list_button_label(db: AsyncSession) -> str:
    return (await _labels(db))["list"][:MAX_BUTTON_TITLE]


async def order_buttons(db: AsyncSession, order_number: str) -> list[Button]:
    lb = await _labels(db)
    return [
        Button(f"ord:{order_number}:done", lb["done"]),
        Button(f"ord:{order_number}:later", lb["later"]),
        Button(f"ord:{order_number}:problem", lb["problem"]),
    ]


async def task_buttons(db: AsyncSession, code: str) -> list[Button]:
    """Only show actions relevant to the current task stage."""
    from app.services import tasks as task_service
    task = await task_service.get_by_code(db, code)
    lb = await _labels(db)
    kind = (task.kind if task else "general").lower()
    if kind == "wash":
        done = "🧼 Wash done" if lb["done"].startswith("✅") else "🧼 Wash ho gaya"
    elif kind == "dry":
        done = "💨 Dry done" if lb["done"].startswith("✅") else "💨 Dry ho gaya"
    elif kind == "iron":
        done = "👔 Iron done" if lb["done"].startswith("✅") else "👔 Iron ho gaya"
    elif kind == "pickup":
        done = "✅ Pickup done" if lb["done"].startswith("✅") else "✅ Pickup ho gaya"
    elif kind == "delivery":
        done = "✅ Delivered" if lb["done"].startswith("✅") else "✅ Delivery ho gayi"
    else:
        done = lb["done"]
    later = "⏳ Delay" if lb["later"].startswith("⏳") else "⏳ Der lagegi"
    return [
        Button(f"task:{code}:done", done[:MAX_BUTTON_TITLE]),
        Button(f"task:{code}:later", later[:MAX_BUTTON_TITLE]),
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


def pieces_text(item: dict) -> str:
    """KG line ke kapde: 'Shirt 5, Pant 3 — 8 pcs'. Na hon to ''."""
    pieces = [p for p in (item.get("pieces") or []) if isinstance(p, dict) and p.get("type")]
    if not pieces:
        return ""
    total = sum(int(p.get("qty") or 0) for p in pieces)
    return ", ".join(f"{p['type']} {int(p.get('qty') or 0)}" for p in pieces) + f" — {total} pcs"


def items_summary(order: Order) -> str:
    """'3 x Shirt, 2 x Pant' from the items JSON.

    KG line ke andar ke kapde bracket mein — washerman ko bore kholte hi
    ginti milani hai, aur grahak ko wapas lete waqt."""
    parts = []
    for it in order.items or []:
        if it.get("kind") == URGENT_KIND:
            continue   # paise ki line hai, kapda nahi — washerman ko "1 x Urgent charge" bekaar
        qty = it.get("qty", 1)
        qty = int(qty) if float(qty).is_integer() else qty
        part = f"{qty} x {it.get('type') or it.get('garment') or it.get('service', '?')}"
        inside = pieces_text(it)
        parts.append(f"{part} ({inside})" if inside else part)
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
