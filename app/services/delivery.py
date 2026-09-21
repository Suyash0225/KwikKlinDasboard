"""Order aage badhana: pickup ho gaya, dhulai taiyar, aur delivery — poori ya thodi.

Partial delivery (BUG_008): grahak ke 12 shirt mein se 8 taiyar hain, ya
darwaze par wo aadhe hi le raha hai. Pehle sirf "Delivered" ka ek button tha
— ya to sab diya maano, ya kuch nahi. Ab har kapde ki line par `delivered`
(kitne diye) likha jaata hai:

  - piece wali line:  {"type": "Shirt", "qty": 12, "delivered": 8}
  - KG bore, ginti ke saath: har kapde par {"type": "Shirt", "qty": 5, "delivered": 3}
  - KG bore, bina ginti: {"delivered_all": true} — bora poora diya ya nahi

Sab kapde diye = order DELIVERED (grahak ko wahi "delivered" message). Kuch
baaki = order OUT_FOR_DELIVERY par rukta hai aur baaki kapde har jagah
"pending" dikhte hain. Urgent charge jaisi paise ki line gini nahi jaati.

Koi migration nahi: sab items JSON mein.
"""

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Order, OrderStatus, Task
from app.models.task import TASK_OPEN
from app.services import audit
from app.services.urgent import is_charge_line

DELIVERABLE = (OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY)
PICKABLE = (OrderStatus.RECEIVED, OrderStatus.PICKUP_ASSIGNED)
WASHABLE = (OrderStatus.RECEIVED, OrderStatus.PICKED_UP, OrderStatus.IN_WASH,
            OrderStatus.IN_DRY, OrderStatus.IN_IRON)


class DeliveryError(ValueError):
    pass


def _int(v) -> int:
    try:
        return max(0, int(round(float(v or 0))))
    except (TypeError, ValueError):
        return 0


def lines(order: Order) -> list[dict]:
    """UI ke liye: har kapde ki line — kitne, kitne diye, kitne baaki."""
    out = []
    for idx, it in enumerate(order.items or []):
        if not isinstance(it, dict) or is_charge_line(it):
            continue
        kg = (it.get("unit") or "").lower() == "kg"
        name = it.get("type") or it.get("garment") or it.get("service") or "?"
        pieces = [p for p in (it.get("pieces") or []) if isinstance(p, dict) and p.get("type")]
        row = {"line": idx, "name": name, "service": it.get("service") or "", "kg": kg,
               "weight": it.get("qty") if kg else None}
        if kg and pieces:
            row["pieces"] = [
                {"piece": j, "type": p["type"], "qty": _int(p.get("qty")),
                 "delivered": min(_int(p.get("delivered")), _int(p.get("qty"))),
                 "pending": max(0, _int(p.get("qty")) - _int(p.get("delivered")))}
                for j, p in enumerate(it.get("pieces") or []) if isinstance(p, dict) and p.get("type")
            ]
            row["qty"] = sum(p["qty"] for p in row["pieces"])
            row["delivered"] = sum(p["delivered"] for p in row["pieces"])
        elif kg:
            row["bag"] = True          # ginti nahi — bora poora diya ya nahi
            row["qty"] = 1
            row["delivered"] = 1 if it.get("delivered_all") else 0
        else:
            row["qty"] = _int(it.get("qty") or 1)
            row["delivered"] = min(_int(it.get("delivered")), row["qty"])
        row["pending"] = max(0, row["qty"] - row["delivered"])
        out.append(row)
    return out


def counts(order: Order) -> dict:
    ls = lines(order)
    total = sum(r["qty"] for r in ls)
    delivered = sum(r["delivered"] for r in ls)
    return {"total": total, "delivered": delivered, "pending": max(0, total - delivered)}


async def _close_tasks(db: AsyncSession, order: Order, kind: str, by: str) -> None:
    from app.services import tasks as task_service

    rows = (
        await db.execute(
            select(Task).where(Task.order_id == order.id, Task.status == TASK_OPEN, Task.kind == kind)
        )
    ).scalars().all()
    for t in rows:
        await task_service.complete_task(db, t, reply=f"{kind} done", by=by)


async def deliver(db: AsyncSession, order: Order, picks: list[dict] | None, *, by: str) -> dict:
    """picks=None: sab kuch. Warna [{"line": i, "qty": n}] ya
    [{"line": i, "piece": j, "qty": n}] (KG bore ke kapde) ya
    [{"line": i, "qty": 1}] (KG bore bina ginti = poora bora)."""
    from app.services.order_service import update_status

    if order.status not in DELIVERABLE:
        raise DeliveryError(f"{order.order_number} is {order.status.name} — only a ready order can be delivered")
    items = deepcopy(order.items or [])
    given = 0

    def give_all(it: dict) -> int:
        n = 0
        if is_charge_line(it):
            return 0
        kg = (it.get("unit") or "").lower() == "kg"
        pieces = [p for p in (it.get("pieces") or []) if isinstance(p, dict) and p.get("type")]
        if kg and pieces:
            for p in pieces:
                n += max(0, _int(p.get("qty")) - _int(p.get("delivered")))
                p["delivered"] = _int(p.get("qty"))
        elif kg:
            if not it.get("delivered_all"):
                it["delivered_all"] = True
                n = 1
        else:
            q = _int(it.get("qty") or 1)
            n = max(0, q - _int(it.get("delivered")))
            it["delivered"] = q
        return n

    if picks is None:
        for it in items:
            if isinstance(it, dict):
                given += give_all(it)
    else:
        for pk in picks:
            try:
                idx = int(pk.get("line"))
                qty = int(pk.get("qty") or 0)
            except (TypeError, ValueError):
                raise DeliveryError("Bad delivery line")
            if qty <= 0:
                continue
            if idx < 0 or idx >= len(items) or not isinstance(items[idx], dict) or is_charge_line(items[idx]):
                raise DeliveryError("That item is not on this bill")
            it = items[idx]
            kg = (it.get("unit") or "").lower() == "kg"
            name = it.get("type") or it.get("service") or "item"
            if pk.get("piece") is not None:
                pieces = it.get("pieces") or []
                j = int(pk["piece"])
                if not kg or j < 0 or j >= len(pieces):
                    raise DeliveryError("That cloth is not in this bag")
                p = pieces[j]
                pending = _int(p.get("qty")) - _int(p.get("delivered"))
                if qty > pending:
                    raise DeliveryError(f"Only {pending} {p.get('type')} left to deliver")
                p["delivered"] = _int(p.get("delivered")) + qty
                given += qty
            elif kg and any(isinstance(p, dict) and p.get("type") for p in (it.get("pieces") or [])):
                raise DeliveryError(f"Pick the clothes inside {name}")
            elif kg:
                if it.get("delivered_all"):
                    raise DeliveryError(f"{name} is already delivered")
                it["delivered_all"] = True
                given += 1
            else:
                pending = _int(it.get("qty") or 1) - _int(it.get("delivered"))
                if qty > pending:
                    raise DeliveryError(f"Only {pending} {name} left to deliver")
                it["delivered"] = _int(it.get("delivered")) + qty
                given += qty
    if given == 0:
        raise DeliveryError("Pick at least one cloth to deliver")

    order.items = items
    flag_modified(order, "items")
    left = counts(order)["pending"]
    if left == 0:
        # grahak ko "delivered + rating" wahi purana message
        await update_status(db, order, OrderStatus.DELIVERED, changed_by=by)   # commits
        await _close_tasks(db, order, "delivery", by)
    else:
        if order.status is OrderStatus.READY:
            # "out for delivery" nahi — neeche wala saaf message jaata hai
            await update_status(db, order, OrderStatus.OUT_FOR_DELIVERY, changed_by=by, notify=False)
        else:
            await db.commit()
        # Grahak ko: aaj kitne kapde diye, kitne abhi dukaan par (API ho to)
        from app.services.order_service import _notify_customer

        await _notify_customer(
            db, order,
            message_key="partial_delivery",
            template_name="kk_partial_delivery",
            template_params=[order.order_number, str(given), str(left)],
            given=str(given),
            pending=str(left),
        )
    await audit.record(
        actor_role="staff", actor=by, action="order_delivered" if left == 0 else "order_partly_delivered",
        args={"order": order.order_number, "given": given, "pending": left},
        result=order.status.name,
    )
    return {"order_number": order.order_number, "delivered_now": given, "pending": left,
            "status": order.status.name}


async def picked_up(db: AsyncSession, order: Order, *, by: str) -> dict:
    from app.services.order_service import update_status

    if order.status not in PICKABLE:
        raise DeliveryError(f"{order.order_number} is {order.status.name} — nothing to pick up")
    await update_status(db, order, OrderStatus.PICKED_UP, changed_by=by)
    await _close_tasks(db, order, "pickup", by)
    return {"order_number": order.order_number, "status": order.status.name}


WASH_STARTABLE = (OrderStatus.RECEIVED, OrderStatus.PICKED_UP)


async def washing(db: AsyncSession, order: Order, *, by: str, notify: bool = False) -> dict:
    """Washerman ne kapde machine mein daale — RECEIVED/PICKED_UP -> IN_WASH."""
    from app.services.order_service import update_status

    if order.status not in WASH_STARTABLE:
        raise DeliveryError(f"{order.order_number} is {order.status.name} — washing cannot start from here")
    await update_status(db, order, OrderStatus.IN_WASH, changed_by=by, notify=notify)
    return {"order_number": order.order_number, "status": order.status.name}


async def ready(db: AsyncSession, order: Order, *, by: str) -> dict:
    from app.services.order_service import update_status

    if order.status not in WASHABLE:
        raise DeliveryError(f"{order.order_number} is {order.status.name} — it cannot be marked ready")
    await update_status(db, order, OrderStatus.READY, changed_by=by)
    return {"order_number": order.order_number, "status": order.status.name}
