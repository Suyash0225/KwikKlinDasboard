from app.utils.dates import today_ist
"""Turnaround — har order kitni der se kis stage par hai, aur kab "delayed" (IMP_006).

Do alag signals:
  stage    current stage kitni der se chal rahi hai — staff reminder ke liye
  promise  grahak ki delivery date (expected_delivery) nikal gayi — actual overdue

Milestones order_status_history se banti hain — naya column nahi: har
status kab aaya, kisne kiya, us stage mein kitne ghante rahe.

Bill banane ki der: dashboard/staff panel "New bill" kholne se Save tak ke
second `orders.bill_seconds` mein (0-3600, galat value chhod dete hain).

Alert: har ghante scheduler naye delayed orders ka EK message manager ko
bhejta hai — har order+stage par ek hi baar (`order_delay_alerted` audit se
yaad rakhte hain), aur audit log mein likhta hai.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderStatus, OrderStatusHistory
from app.services import app_settings

log = structlog.get_logger()

TERMINAL = (OrderStatus.DELIVERED, OrderStatus.CANCELLED)
STAGE_LABEL = {
    "RECEIVED": "Received", "PICKUP_ASSIGNED": "Pickup", "PICKED_UP": "Picked up",
    "IN_WASH": "Washing", "IN_DRY": "Drying", "IN_IRON": "Ironing", "READY": "Ready",
    "OUT_FOR_DELIVERY": "Out for delivery", "DELIVERED": "Delivered",
    "CANCELLED": "Cancelled", "ON_HOLD": "On hold",
}


async def limits(db: AsyncSession) -> dict:
    raw = await app_settings.get(db, "stage_limit_hours") or {}
    default = app_settings.DEFAULTS["stage_limit_hours"]
    out = {}
    for k, v in {**default, **(raw if isinstance(raw, dict) else {})}.items():
        try:
            out[k] = max(1, int(v))
        except (TypeError, ValueError):
            out[k] = default.get(k, 24)
    return out


def _hours(a: datetime, b: datetime) -> float:
    return round(max(0.0, (b - a).total_seconds()) / 3600, 1)


async def histories(db: AsyncSession, order_ids) -> dict:
    """order_id -> [history rows, puraane pehle] — ek hi query."""
    out: dict = {}
    if not order_ids:
        return out
    rows = (
        await db.execute(
            select(OrderStatusHistory)
            .where(OrderStatusHistory.order_id.in_(list(order_ids)))
            .order_by(OrderStatusHistory.changed_at)
        )
    ).scalars().all()
    for r in rows:
        out.setdefault(r.order_id, []).append(r)
    return out


def track(order: Order, history: list, stage_limits: dict, now: datetime | None = None) -> dict:
    """Ek order ki poori tasveer — milestones, abhi ki stage ka time, deri."""
    now = now or datetime.now(timezone.utc)
    if not history:
        # Purana/import kiya order jiska history row nahi — bill ka waqt hi
        # pehla (aur abhi tak ka) milestone
        history = [SimpleNamespace(new_status=order.status, changed_at=order.created_at, changed_by="bill")]
    milestones = []
    for i, h in enumerate(history):
        end = history[i + 1].changed_at if i + 1 < len(history) else None
        milestones.append({
            "status": h.new_status.name, "label": STAGE_LABEL.get(h.new_status.name, h.new_status.name),
            "at": h.changed_at.isoformat(), "by": h.changed_by,
            "hours": _hours(h.changed_at, end or now) if (end or order.status not in TERMINAL) else None,
            "limit": stage_limits.get(h.new_status.name),
        })
        if milestones[-1]["hours"] is not None and milestones[-1]["limit"]:
            milestones[-1]["late"] = milestones[-1]["hours"] > milestones[-1]["limit"]
    since = history[-1].changed_at
    status = order.status.name
    stage_hours = _hours(since, now)
    limit = stage_limits.get(status)
    active = order.status not in TERMINAL
    stage_late = bool(active and limit and stage_hours > limit)
    promise_late = bool(active and order.expected_delivery and order.expected_delivery < today_ist())
    due_today = bool(active and order.expected_delivery == today_ist()
                     and order.status not in (OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY))
    total_hours = _hours(order.created_at, order.actual_delivery or now)
    reasons = []
    if promise_late:
        reasons.append(f"Delivery date {order.expected_delivery.strftime('%d %b')} passed")
    if stage_late:
        reasons.append(f"{STAGE_LABEL.get(status, status)} for {stage_hours:g}h (limit {limit}h)")
    return {
        "stage": status, "stage_label": STAGE_LABEL.get(status, status),
        "stage_since": since.isoformat(), "stage_hours": stage_hours, "stage_limit": limit,
        "stage_late": stage_late, "promise_late": promise_late, "due_today": due_today,
        # Keep the customer promise and internal stage timer as separate flags,
        # but report both reasons when both deadlines have been missed.
        "delayed": promise_late,
        "delay_reason": "; ".join(reasons),
        "total_hours": total_hours,
        "bill_seconds": order.bill_seconds,
        "milestones": milestones,
    }


async def track_many(db: AsyncSession, orders: list[Order]) -> dict:
    """order_id -> track() — dashboard list ke liye ek saath."""
    hist = await histories(db, [o.id for o in orders])
    lim = await limits(db)
    now = datetime.now(timezone.utc)
    return {o.id: track(o, hist.get(o.id, []), lim, now) for o in orders}


def clean_bill_seconds(v) -> int | None:
    """Browser ka bheja time — sirf samajhdaar value (1s .. 1 ghanta)."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 3600 else None


async def run_delay_alerts(db: AsyncSession) -> int:
    """Naye delayed orders manager ko — har order+stage par ek baar. Lautata
    hai kitne naye delay mile."""
    from app.models import AuditLog, Customer
    from app.services import audit, team

    orders = (
        await db.execute(select(Order).where(Order.status.notin_(TERMINAL)).limit(500))
    ).scalars().all()
    if not orders:
        return 0
    tracked = await track_many(db, orders)
    fresh = []
    for o in orders:
        t = tracked[o.id]
        if not (t["promise_late"] or t["stage_late"]):
            continue
        key = f"{o.order_number}:{t['stage']}:delay"
        # Preserve idempotency for rows written by the older promise-only
        # alert implementation, while allowing stage-only delays to alert too.
        legacy_keys = [
            key,
            f"{o.order_number}:{t['stage']}:promise",
            f"{o.order_number}:{t['stage']}:stage",
        ]
        seen = (
            await db.execute(
                select(AuditLog.id).where(
                    AuditLog.action == "order_delayed", AuditLog.result.in_(legacy_keys)
                ).limit(1)
            )
        ).scalar_one_or_none()
        if seen is not None:
            continue
        cust = await db.get(Customer, o.customer_id)
        fresh.append((o, t, key, (cust.name or cust.phone) if cust else ""))
    for o, t, key, who in fresh:
        await audit.record(
            actor_role="system", actor="turnaround", action="order_delayed",
            args={"order": o.order_number, "stage": t["stage"], "hours": t["stage_hours"],
                  "limit": t["stage_limit"], "reason": t["delay_reason"]},
            result=key,
        )
    if fresh:
        lines = [f"• {o.order_number} {who} — {t['delay_reason']}" for o, t, _, who in fresh[:10]]
        more = f"\n…aur {len(fresh) - 10}" if len(fresh) > 10 else ""
        await team.notify_admins(db, "⏱ Delayed orders:\n" + "\n".join(lines) + more)
        log.info("delay_alerts_sent", count=len(fresh))
    return len(fresh)
