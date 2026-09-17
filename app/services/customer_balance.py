"""Grahak ka pichhla hisaab — paisa bhi, kapde bhi.

Naye bill par sirf "Previous due ₹X" kaafi nahi tha: pichhle order ke 5
kapde dukaan par ruke hon to grahak ko wo bhi saaf dikhna chahiye (WhatsApp
text, bill page, statement). Ek hi jagah hisaab, teeno jagah wahi ankde.
Tenant filter ORM se apne aap (database.py) — doosri dukaan ka kuch nahi judta.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderStatus


async def previous(db: AsyncSession, customer_id, *, exclude_order_id=None) -> dict:
    """{"due", "bills", "clothes": [{"order", "name", "qty"}], "clothes_total", "orders": [...]}"""
    from app.services import delivery

    q = select(Order).where(
        Order.customer_id == customer_id,
        Order.status != OrderStatus.CANCELLED,
    ).order_by(Order.created_at)
    if exclude_order_id is not None:
        q = q.where(Order.id != exclude_order_id)
    rows = (await db.execute(q)).scalars().all()
    due, bills, clothes, orders = 0.0, 0, [], []
    for o in rows:
        d = float(o.total_amount or 0) - float(o.amount_paid or 0) if o.total_amount is not None else 0.0
        pending = []
        if o.status is not OrderStatus.DELIVERED:
            for line in delivery.lines(o):
                if line.get("pieces"):
                    pending += [(p["type"], p["pending"]) for p in line["pieces"] if p["pending"]]
                elif line["pending"]:
                    pending.append((line["name"], line["pending"]))
        if d > 0.009:
            due += d
            bills += 1
        for name, qty in pending:
            clothes.append({"order": o.order_number, "name": name, "qty": int(qty)})
        if d > 0.009 or pending:
            orders.append({"order": o.order_number, "due": round(max(d, 0.0), 2),
                           "pending": sum(int(q) for _, q in pending), "status": o.status.name})
    return {
        "due": round(due, 2), "bills": bills, "clothes": clothes,
        "clothes_total": sum(c["qty"] for c in clothes), "orders": orders,
    }
