"""Staff ki zimmedari aur shabashi — grahak ki rating se points, late kaam ki yaad.

Teen hisse:

1. **Kisne kaam kiya** (`workers`). Order par status history batati hai ki
   asal mein kisne dhoya (IN_WASH/READY) aur kisne pahunchaya (DELIVERED).
   Wo na mile to order ka assigned_washer / assigned_delivery. History mein
   naam kabhi "Ravi" aur kabhi "staff:Ravi" likha hota hai — dono maane.

2. **Rating -> points** (`record_rating`). Rating sirf washing ki hai:
   delivery ke message mein grahak ko bataate hain kisne kapde taiyaar
   kiye. Wo "Bahut achha" dabaye to us washerman ko points, staff ko turant shabashi, aur mahine ke points ek padav (50, 100…)
   paar karein to owner ko bhi. "Sudhar chahiye" par points nahi, staff ko
   seedhi par izzat wali baat. Ek order + ek aadmi = ek hi baar (unique).

   Grahak ko "inaam milta hai" tabhi likha jaata hai jab owner ne Settings
   mein inaam chalu kiya ho (`staff_reward_enabled`). Jhootha vaada grahak
   aur staff dono ka bharosa todta hai.

3. **Late kaam ki yaad** (`run_work_nudges`, scheduler se). Jinke vaade ki
   date aaj ya beet chuki, unke zimmedar aadmi ko ek hi message mein saari
   list. Lehja saaf zimmedari ka hai — kitni der, customer par kya asar, kaam
   kiske naam par — dhamki ya tankhwah kaatne ki baat nahi. Do din se zyada
   late ho to manager ko bhi. Shaam ko jisne aaj sab samay par kiya, use
   shabashi (`run_day_thanks`).

Bhejna: dukaan ka WhatsApp (send_message) + phone notification (web_push).
Dono kabhi raise nahi karte — kaam ki yaad kisi order ko nahi giraati.
"""

from datetime import date, datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer, Order, OrderStatus, OrderStatusHistory, Staff, StaffPoint, StaffRole
from app.services.whatsapp import SendError, send_message

log = structlog.get_logger()

IST = timezone(timedelta(hours=5, minutes=30))

WASH_DONE = (OrderStatus.IN_WASH, OrderStatus.IN_DRY, OrderStatus.IN_IRON, OrderStatus.READY)
ROAD_DONE = (OrderStatus.DELIVERED, OrderStatus.OUT_FOR_DELIVERY)
WASH_STAGES = (OrderStatus.RECEIVED, OrderStatus.PICKED_UP, OrderStatus.IN_WASH, OrderStatus.IN_DRY, OrderStatus.IN_IRON)
ROAD_STAGES = (OrderStatus.PICKUP_ASSIGNED, OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY)
# Rating kis delivery ki hai: grahak ka sabse naya delivered order, itne din tak
RATING_WINDOW = timedelta(days=7)
# Mahine ke points ke padav jahan owner ko bhi khabar jaati hai
MILESTONE_STEP = 50
# Itne din late = manager ko bhi
MANAGER_LATE_DAYS = 2

_STATUS_WORD = {
    OrderStatus.RECEIVED: "received", OrderStatus.PICKUP_ASSIGNED: "pickup",
    OrderStatus.PICKED_UP: "picked up", OrderStatus.IN_WASH: "washing",
    OrderStatus.IN_DRY: "drying", OrderStatus.IN_IRON: "ironing",
    OrderStatus.READY: "ready — deliver", OrderStatus.OUT_FOR_DELIVERY: "out for delivery",
}


def first_name(name: str | None) -> str:
    return ((name or "").strip().split() or [""])[0]


def _clean_actor(changed_by: str | None) -> str:
    s = (changed_by or "").strip()
    return s[6:].strip() if s.lower().startswith("staff:") else s


async def workers(db: AsyncSession, order: Order) -> dict[str, Staff]:
    """{"washer": Staff, "delivery": Staff} — jo mile. Ek hi aadmi dono ho sakta hai."""
    rows = (
        await db.execute(
            select(OrderStatusHistory.changed_by, OrderStatusHistory.new_status)
            .where(OrderStatusHistory.order_id == order.id)
            .order_by(OrderStatusHistory.changed_at.desc())
        )
    ).all()
    names: dict[str, str] = {}
    for who, status in rows:
        role = "washer" if status in WASH_DONE else "delivery" if status in ROAD_DONE else None
        actor = _clean_actor(who)
        if role and actor and role not in names:
            names[role] = actor
    staff = (await db.execute(select(Staff).where(Staff.is_active))).scalars().all()
    by_name = {s.name.strip().lower(): s for s in staff}
    by_id = {s.id: s for s in staff}
    out: dict[str, Staff] = {}
    for role, col in (("washer", order.assigned_washer_id), ("delivery", order.assigned_delivery_id)):
        s = by_name.get((names.get(role) or "").lower()) or by_id.get(col)
        # owner (ADMIN) ne khud status badla ho to wo "kaam karne wala" nahi
        if s is not None and s.role is not StaffRole.ADMIN:
            out[role] = s
    return out


async def _setting(db: AsyncSession, key: str):
    from app.services import app_settings

    return await app_settings.get(db, key)


async def staff_line(db: AsyncSession, order: Order, lang: str) -> str:
    """Delivery message ki line: "Aapke kapde Ravi ne taiyaar kiye hain."

    Rating sirf washing ki hoti hai, isliye naam sirf washerman ka —
    delivery wale ka naam grahak ko ajeeb lagta tha ("kisne pahunchaya").
    """
    from app.services.messages import get_message

    try:
        if not await _setting(db, "rating_show_staff_names"):
            return ""
        washer = (await workers(db, order)).get("washer")
        if washer is None:
            return ""
        note = get_message(
            "rating_reward_note" if await _setting(db, "staff_reward_enabled") else "rating_points_note",
            lang=lang,
        )
        return get_message("rating_staff_line", lang=lang, name=first_name(washer.name), points_note=note)
    except Exception:
        log.exception("rating_staff_line_failed")
    return ""


async def month_points(db: AsyncSession, staff_id) -> int:
    start = datetime.now(IST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(
        (
            await db.execute(
                select(func.coalesce(func.sum(StaffPoint.points), 0)).where(
                    StaffPoint.staff_id == staff_id, StaffPoint.created_at >= start
                )
            )
        ).scalar_one()
    )


async def record_rating(db: AsyncSession, customer: Customer, kind: str) -> dict | None:
    """Grahak ki rating uske sabse naye delivered order ke staff par. Commits.

    None: koi haal ka delivered order nahi, us par pehle hi rating aa chuki,
    ya kisi staff ka pata nahi. Warna {"order", "points", "names", "awarded"}.
    """
    if kind not in ("good", "mid", "bad"):
        return None
    since = datetime.now(timezone.utc) - RATING_WINDOW
    order = (
        await db.execute(
            select(Order)
            .where(
                Order.customer_id == customer.id,
                Order.status == OrderStatus.DELIVERED,
                func.coalesce(Order.actual_delivery, Order.updated_at) >= since,
            )
            .order_by(func.coalesce(Order.actual_delivery, Order.updated_at).desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if order is None:
        return None
    already = (
        await db.execute(select(func.count()).select_from(StaffPoint).where(StaffPoint.order_id == order.id))
    ).scalar_one()
    if already:
        return None
    # rating washing ki hai — points us washerman ke jisne ye kapde taiyaar kiye
    washer = (await workers(db, order)).get("washer")
    if washer is None:
        return None
    pts = {"good": int(await _setting(db, "staff_points_good") or 0),
           "mid": int(await _setting(db, "staff_points_mid") or 0), "bad": 0}[kind]
    db.add(StaffPoint(staff_id=washer.id, order_id=order.id, rating=kind, role="washer", points=pts))
    awarded: list[tuple[Staff, str]] = [(washer, "washer")]
    from sqlalchemy.exc import IntegrityError

    try:
        await db.commit()
    except IntegrityError:          # do jawab ek saath aaye — ek hi gina
        await db.rollback()
        return None
    return {
        "order": order.order_number,
        "order_id": order.id,
        "points": pts,
        "kind": kind,
        "names": [first_name(s.name) for s, _ in awarded],
        "awarded": [(s.id, s.name, s.phone) for s, _ in awarded],
        "customer": first_name(customer.name) or "Customer",
    }


async def _tell_staff(db: AsyncSession, staff_id, phone: str, text: str, *, title: str, tag: str) -> None:
    """Dukaan ke WhatsApp se, aur phone notification se. Kabhi raise nahi."""
    from app.services import tenant_context, web_push

    try:
        await send_message(db, to_phone=phone, text=text)
    except SendError as exc:
        log.info("staff_message_not_sent", error=str(exc)[:120])
    except Exception:
        log.exception("staff_message_failed")
    try:
        await web_push.notify(
            db, tenant_context.effective_tenant_id(), staff_ids=[staff_id],
            title=title, body=text.split("\n", 1)[0][:180], tag=tag,
        )
    except Exception:
        log.exception("staff_push_failed")


async def _reward_line(db: AsyncSession) -> str:
    from app.services.messages import get_message

    note = (await _setting(db, "staff_reward_note") or "").strip()
    if not (await _setting(db, "staff_reward_enabled") and note):
        return ""
    return get_message("staff_reward_line", note=note[:150])


async def thank_staff(db: AsyncSession, result: dict) -> None:
    """Rating ke baad har us aadmi ko jisne kaam kiya — shabashi ya sudhaar."""
    from app.services import team
    from app.services.messages import get_message

    kind = result["kind"]
    reward = await _reward_line(db)
    for staff_id, name, phone in result["awarded"]:
        total = await month_points(db, staff_id)
        key = {"good": "staff_rating_good", "mid": "staff_rating_mid", "bad": "staff_rating_bad"}[kind]
        text = get_message(
            key, name=first_name(name), customer=result["customer"], order_number=result["order"],
            points=str(result["points"]), month_points=str(total), reward_line=reward,
        )
        title = {"good": "🌟 Customer loved your work", "mid": "🙂 New rating", "bad": "⚠️ Customer feedback"}[kind]
        await _tell_staff(db, staff_id, phone, text, title=title, tag=f"rating-{result['order']}")
        before = total - result["points"]
        if result["points"] > 0 and before // MILESTONE_STEP < total // MILESTONE_STEP:
            await _tell_staff(
                db, staff_id, phone,
                get_message("staff_points_milestone", name=first_name(name), month_points=str(total), reward_line=reward),
                title="🏆 Points milestone", tag="milestone",
            )
            await team.notify_admins(db, f"🏆 {name}: is mahine {total} points (customer ratings se).", skip_phone=phone)


# ------------------------------------------------------------ kaam ki yaad ----


def _late_line(o: Order, cust: Customer | None, today: date) -> str:
    days = (today - o.expected_delivery).days if o.expected_delivery else 0
    when = "aaj" if days == 0 else f"{days} din late"
    return f"• {o.order_number} — {first_name(cust.name if cust else '') or 'Customer'} — {when} ({_STATUS_WORD.get(o.status, o.status.name.lower())})"


async def _responsible(db: AsyncSession) -> dict[str, Staff | None]:
    """Default log jab order kisi ke naam na ho: dukaan ka washer / delivery."""
    from app.services import team

    out: dict[str, Staff | None] = {"washer": None, "delivery": None}
    try:
        out["delivery"] = await team.delivery_staff(db)
    except Exception:
        pass
    phone = (await _setting(db, "default_washer_phone") or "").strip()
    if phone:
        out["washer"] = (await db.execute(select(Staff).where(Staff.phone == phone, Staff.is_active))).scalar_one_or_none()
    return out


async def due_and_late(db: AsyncSession, today: date) -> dict:
    """{staff_id: {"staff", "late": [(order, cust)], "due": [...]}} — aaj tak ke vaade wale chalte order."""
    rows = (
        await db.execute(
            select(Order).where(
                Order.status.in_(WASH_STAGES + ROAD_STAGES),
                Order.expected_delivery.is_not(None),
                Order.expected_delivery <= today,
            ).order_by(Order.expected_delivery, Order.created_at).limit(500)
        )
    ).scalars().all()
    if not rows:
        return {}
    staff = {s.id: s for s in (await db.execute(select(Staff).where(Staff.is_active))).scalars().all()}
    custs = {
        c.id: c for c in (
            await db.execute(select(Customer).where(Customer.id.in_({o.customer_id for o in rows})))
        ).scalars().all()
    }
    defaults = await _responsible(db)
    out: dict = {}
    for o in rows:
        road = o.status in ROAD_STAGES
        s = staff.get(o.assigned_delivery_id if road else o.assigned_washer_id) or defaults["delivery" if road else "washer"]
        if s is None or s.role is StaffRole.ADMIN:
            continue
        slot = out.setdefault(s.id, {"staff": s, "late": [], "due": []})
        slot["late" if o.expected_delivery < today else "due"].append((o, custs.get(o.customer_id)))
    return out


async def run_work_nudges(db: AsyncSession, now_ist: datetime, claim) -> int:
    """Ek nudge ghante par: har zimmedar aadmi ko uske late (aur pehle ghante par aaj ke) kaam.

    `claim(key) -> bool` scheduler ka dedup — ek aadmi ko ek ghante mein ek hi.
    """
    from app.services import team
    from app.services.messages import get_message

    if not await _setting(db, "staff_nudges_enabled"):
        return 0
    from app.config import settings

    # raat ke chup ghante (QUIET_HOURS) mein kabhi nahi — owner kuch bhi ghanta likhe
    q0, q1 = settings.QUIET_HOURS_START, settings.QUIET_HOURS_END
    quiet = (lambda h: h >= q0 or h < q1) if q0 > q1 else (lambda h: q0 <= h < q1)
    hours = sorted(int(h) for h in (await _setting(db, "staff_nudge_hours") or []) if not quiet(int(h)))
    if not hours or now_ist.hour not in hours:
        return 0
    first_slot = now_ist.hour == hours[0]
    today = now_ist.date()
    work = await due_and_late(db, today)
    sent = 0
    very_late = []
    for sid, slot in work.items():
        s = slot["staff"]
        name = first_name(s.name) or s.name
        if slot["late"] and await claim(f"nudge:{today}:{now_ist.hour}:{sid}"):
            lines = "\n".join(_late_line(o, c, today) for o, c in slot["late"][:12])
            text = get_message("staff_nudge_late", name=name, count=str(len(slot["late"])), lines=lines)
            await _tell_staff(db, s.id, s.phone, text, title=f"⏰ {len(slot['late'])} jobs late", tag="nudge-late")
            sent += 1
        # aaj ke kaam ki yaad din mein ek baar, subah ke pehle slot par
        if first_slot and slot["due"] and await claim(f"nudge-due:{today}:{sid}"):
            lines = "\n".join(_late_line(o, c, today) for o, c in slot["due"][:12])
            text = get_message("staff_nudge_due", name=name, count=str(len(slot["due"])), lines=lines)
            await _tell_staff(db, s.id, s.phone, text, title=f"📋 {len(slot['due'])} jobs due today", tag="nudge-due")
            sent += 1
        very_late += [
            (o, c, s) for o, c in slot["late"] if (today - o.expected_delivery).days >= MANAGER_LATE_DAYS
        ]
    if very_late and first_slot and await claim(f"nudge-mgr:{today}"):
        lines = "\n".join(f"{_late_line(o, c, today)} · {first_name(s.name)}" for o, c, s in very_late[:15])
        await team.notify_admins(
            db, get_message("manager_late_digest", days=str(MANAGER_LATE_DAYS), count=str(len(very_late)), lines=lines)
        )
    return sent


async def run_day_thanks(db: AsyncSession, now_ist: datetime, claim) -> int:
    """Shaam ko: jisne aaj kam se kam 3 kaam aage badhaye aur jiska kuch late nahi — shabashi."""
    from app.services.messages import get_message

    if not await _setting(db, "staff_nudges_enabled"):
        return 0
    if now_ist.hour != int(await _setting(db, "staff_day_thanks_hour") or 20):
        return 0
    today = now_ist.date()
    start = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    counts = dict(
        (
            await db.execute(
                select(OrderStatusHistory.changed_by, func.count())
                .join(Order, Order.id == OrderStatusHistory.order_id)
                .where(
                    OrderStatusHistory.changed_at >= start,
                    OrderStatusHistory.new_status.in_(WASH_DONE + ROAD_DONE + (OrderStatus.PICKED_UP,)),
                )
                .group_by(OrderStatusHistory.changed_by)
            )
        ).all()
    )
    done: dict[str, int] = {}
    for who, n in counts.items():
        k = _clean_actor(who).lower()
        done[k] = done.get(k, 0) + n
    work = await due_and_late(db, today)
    late_ids = {sid for sid, slot in work.items() if slot["late"]}
    sent = 0
    for s in (await db.execute(select(Staff).where(Staff.is_active))).scalars().all():
        n = done.get(s.name.strip().lower(), 0)
        if s.role is StaffRole.ADMIN or n < 3 or s.id in late_ids:
            continue
        if await claim(f"daythanks:{today}:{s.id}"):
            text = get_message("staff_day_thanks", name=first_name(s.name) or s.name, count=str(n))
            await _tell_staff(db, s.id, s.phone, text, title="🌟 Great work today", tag="day-thanks")
            sent += 1
    return sent
