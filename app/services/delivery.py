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


def is_kg(it: dict) -> bool:
    """KG line? Purane WhatsApp bill unit nahi likhte the; 2.5 jaisi adhoori
    ginti kapdon ki nahi hoti — wo wazan hai."""
    unit = (it.get("unit") or "").lower()
    return unit == "kg" or (not unit and _fractional(it.get("qty")))


def _fractional(v) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f > 0 and f != int(f)


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
        kg = is_kg(it)
        name = it.get("type") or it.get("garment") or it.get("service") or "?"
        pieces = [p for p in (it.get("pieces") or []) if isinstance(p, dict) and p.get("type")]
        row = {"line": idx, "name": name, "service": it.get("service") or "", "kg": kg,
               "weight": it.get("qty") if kg else None}
        if kg and pieces:
            row["pieces"] = []
            for j, p in enumerate(it.get("pieces") or []):
                if not (isinstance(p, dict) and p.get("type")):
                    continue
                q, d = _int(p.get("qty")), min(_int(p.get("delivered")), _int(p.get("qty")))
                h = min(_int(p.get("held")), q - d)
                row["pieces"].append({"piece": j, "type": p["type"], "qty": q, "delivered": d,
                                      "pending": q - d, "held": h, "deliverable": q - d - h})
            row["qty"] = sum(p["qty"] for p in row["pieces"])
            row["delivered"] = sum(p["delivered"] for p in row["pieces"])
            row["held"] = sum(p["held"] for p in row["pieces"])
        elif kg:
            row["bag"] = True          # ginti nahi — bora poora diya ya nahi
            row["qty"] = 1
            row["delivered"] = 1 if it.get("delivered_all") else 0
            row["held"] = 1 if it.get("held_all") and not it.get("delivered_all") else 0
        else:
            row["qty"] = _int(it.get("qty") or 1)
            row["delivered"] = min(_int(it.get("delivered")), row["qty"])
            row["held"] = min(_int(it.get("held")), row["qty"] - row["delivered"])
        row["pending"] = max(0, row["qty"] - row["delivered"])
        # Rok kar rakhe kapde (washerman: "ye abhi nahi hua") delivery mein nahi jaate
        row["deliverable"] = max(0, row["pending"] - row["held"])
        if row["held"] and it.get("hold_reason"):
            row["hold_reason"] = it["hold_reason"]
        out.append(row)
    return out


def counts(order: Order) -> dict:
    ls = lines(order)
    total = sum(r["qty"] for r in ls)
    delivered = sum(r["delivered"] for r in ls)
    held = sum(r["held"] for r in ls)
    out = {"total": total, "delivered": delivered, "pending": max(0, total - delivered)}
    if held:   # sirf jab sach mein kuch roka ho — purana jawab waisa hi rahe
        out["held"] = held
    return out


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
        kg = is_kg(it)
        pieces = [p for p in (it.get("pieces") or []) if isinstance(p, dict) and p.get("type")]
        # "Sab do" = jo dene layak hai; roke hue kapde dukaan par hi rehte hain
        if kg and pieces:
            for p in pieces:
                give = max(0, _int(p.get("qty")) - _int(p.get("delivered")) - _int(p.get("held")))
                n += give
                p["delivered"] = _int(p.get("delivered")) + give
        elif kg:
            if not it.get("delivered_all") and not it.get("held_all"):
                it["delivered_all"] = True
                n = 1
        else:
            q = _int(it.get("qty") or 1)
            n = max(0, q - _int(it.get("delivered")) - _int(it.get("held")))
            it["delivered"] = _int(it.get("delivered")) + n
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
            kg = is_kg(it)
            name = it.get("type") or it.get("service") or "item"
            if pk.get("piece") is not None:
                pieces = it.get("pieces") or []
                j = int(pk["piece"])
                if not kg or j < 0 or j >= len(pieces):
                    raise DeliveryError("That cloth is not in this bag")
                p = pieces[j]
                pending = _int(p.get("qty")) - _int(p.get("delivered")) - _int(p.get("held"))
                if qty > pending:
                    raise DeliveryError(_left_msg(pending, p.get("type"), _int(p.get("held"))))
                p["delivered"] = _int(p.get("delivered")) + qty
                given += qty
            elif kg and any(isinstance(p, dict) and p.get("type") for p in (it.get("pieces") or [])):
                raise DeliveryError(f"Pick the clothes inside {name}")
            elif kg:
                if it.get("delivered_all"):
                    raise DeliveryError(f"{name} is already delivered")
                if it.get("held_all"):
                    raise DeliveryError(f"{name} is on hold at the shop")
                it["delivered_all"] = True
                given += 1
            else:
                pending = _int(it.get("qty") or 1) - _int(it.get("delivered")) - _int(it.get("held"))
                if qty > pending:
                    raise DeliveryError(_left_msg(pending, name, _int(it.get("held"))))
                it["delivered"] = _int(it.get("delivered")) + qty
                given += qty
    if given == 0:
        if counts(order).get("held", 0):
            raise DeliveryError("Everything left is on hold at the shop — nothing to deliver yet")
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


def _left_msg(pending: int, name, held: int) -> str:
    msg = f"Only {max(0, pending)} {name} left to deliver"
    return f"{msg} ({held} on hold at the shop)" if held else msg


# ------------------------------------------------ kapda rokna (hold) ----
# Washerman: "ye kapda abhi nahi hua" (daag nahi gaya, sukha nahi, button
# toota). Baaki order aage badhta hai; roka hua kapda delivery mein nahi
# jaata aur order tab tak DELIVERED nahi hota jab tak wo bhi na pahunche —
# bilkul partial delivery jaisa.
HOLDABLE = WASHABLE + (OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY)


def _apply(items: list, picks: list[dict] | None, *, hold: bool) -> list[str]:
    """picks ko items par lagao. Lautata hai kya badla ("Shirt ×2")."""
    changed: list[str] = []

    def one(it: dict, piece: dict | None, qty: int | None) -> None:
        target = piece if piece is not None else it
        name = (piece or {}).get("type") or it.get("type") or it.get("garment") or it.get("service") or "item"
        kg_bag = piece is None and is_kg(it)
        if kg_bag:
            if any(isinstance(p, dict) and p.get("type") for p in (it.get("pieces") or [])):
                raise DeliveryError(f"Pick the clothes inside {name}")
            if hold:
                if it.get("delivered_all") or it.get("held_all"):
                    raise DeliveryError(f"{name} cannot be put on hold")
                it["held_all"] = True
            else:
                if not it.get("held_all"):
                    return
                it.pop("held_all", None)
            changed.append(f"{name} (bag)")
            return
        q, d, h = _int(target.get("qty") or 1), _int(target.get("delivered")), _int(target.get("held"))
        if hold:
            free = q - d - h
            n = free if qty is None else qty
            if n <= 0:
                return
            if n > free:
                raise DeliveryError(f"Only {free} {name} can be put on hold")
            target["held"] = h + n
        else:
            n = h if qty is None else min(qty, h)
            if n <= 0:
                return
            target["held"] = h - n
            if not target["held"]:
                target.pop("held", None)
        changed.append(f"{name} ×{n}")

    if picks is None:          # sirf release: sab chhodo
        for it in items:
            if not isinstance(it, dict) or is_charge_line(it):
                continue
            pieces = [p for p in (it.get("pieces") or []) if isinstance(p, dict) and p.get("type")]
            for p in pieces:
                one(it, p, None)
            if not pieces:
                one(it, None, None)
        return changed
    for pk in picks:
        try:
            idx = int(pk.get("line"))
            qty = int(pk.get("qty") or 0)
        except (TypeError, ValueError):
            raise DeliveryError("Bad cloth line")
        if qty <= 0:
            continue
        if idx < 0 or idx >= len(items) or not isinstance(items[idx], dict) or is_charge_line(items[idx]):
            raise DeliveryError("That item is not on this bill")
        it = items[idx]
        if pk.get("piece") is not None:
            pieces = it.get("pieces") or []
            j = int(pk["piece"])
            if j < 0 or j >= len(pieces) or not isinstance(pieces[j], dict):
                raise DeliveryError("That cloth is not in this bag")
            one(it, pieces[j], qty)
        else:
            one(it, None, qty)
    return changed


def _note(order: Order, by: str, text: str) -> None:
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %H:%M")
    line = f"[{stamp} {by}] {text}"
    order.notes = f"{order.notes}\n{line}" if order.notes else line


async def hold(db: AsyncSession, order: Order, picks: list[dict], *, reason: str, by: str) -> dict:
    """Kuch kapde rok do — status nahi badalta, wo washerman alag se badhata hai."""
    if order.status not in HOLDABLE:
        raise DeliveryError(f"{order.order_number} is {order.status.name} — clothes cannot be held now")
    reason = " ".join((reason or "").split())[:120]
    if not reason:
        raise DeliveryError("Write why these clothes are on hold")
    items = deepcopy(order.items or [])
    changed = _apply(items, picks, hold=True)
    if not changed:
        raise DeliveryError("Pick at least one cloth to hold")
    for pk in picks:
        idx = int(pk.get("line"))
        if 0 <= idx < len(items) and isinstance(items[idx], dict):
            items[idx]["hold_reason"] = reason
    order.items = items
    flag_modified(order, "items")
    _note(order, by, f"⏸ On hold: {', '.join(changed)} — {reason}")
    db.add(order)
    await db.commit()
    await audit.record(
        actor_role="staff", actor=by, action="clothes_held",
        args={"order": order.order_number, "clothes": changed, "reason": reason},
        result=order.status.name,
    )
    return {"order_number": order.order_number, "held": changed, "clothes": counts(order)}


async def release(db: AsyncSession, order: Order, picks: list[dict] | None, *, by: str) -> dict:
    """Roka hua kaam ho gaya — kapde wapas delivery ke liye taiyaar."""
    if order.status in (OrderStatus.DELIVERED, OrderStatus.CANCELLED):
        raise DeliveryError(f"{order.order_number} is {order.status.name}")
    items = deepcopy(order.items or [])
    changed = _apply(items, picks, hold=False)
    if not changed:
        raise DeliveryError("Nothing is on hold")
    for it in items:
        if isinstance(it, dict) and "hold_reason" in it and not (
            _int(it.get("held")) or it.get("held_all")
            or any(_int(p.get("held")) for p in (it.get("pieces") or []) if isinstance(p, dict))
        ):
            it.pop("hold_reason", None)
    order.items = items
    flag_modified(order, "items")
    _note(order, by, f"▶ Hold done: {', '.join(changed)}")
    db.add(order)
    await db.commit()
    await audit.record(
        actor_role="staff", actor=by, action="clothes_released",
        args={"order": order.order_number, "clothes": changed}, result=order.status.name,
    )
    return {"order_number": order.order_number, "released": changed, "clothes": counts(order)}


def has_held(order: Order) -> bool:
    return counts(order).get("held", 0) > 0


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


IRON_STARTABLE = (OrderStatus.RECEIVED, OrderStatus.PICKED_UP, OrderStatus.IN_WASH, OrderStatus.IN_DRY)


async def ironing(db: AsyncSession, order: Order, *, by: str) -> dict:
    """Dhulai ho gayi, kapde press wale ke paas — -> IN_IRON.

    RECEIVED/PICKED_UP se bhi chalta hai: sirf-press wale order dhulai se
    guzarte hi nahi.
    """
    from app.services.order_service import update_status

    if order.status not in IRON_STARTABLE:
        raise DeliveryError(f"{order.order_number} is {order.status.name} — ironing cannot start from here")
    await update_status(db, order, OrderStatus.IN_IRON, changed_by=by)
    return {"order_number": order.order_number, "status": order.status.name}


async def ready(db: AsyncSession, order: Order, *, by: str) -> dict:
    from app.services.order_service import update_status

    if order.status not in WASHABLE:
        raise DeliveryError(f"{order.order_number} is {order.status.name} — it cannot be marked ready")
    await update_status(db, order, OrderStatus.READY, changed_by=by)
    return {"order_number": order.order_number, "status": order.status.name}
