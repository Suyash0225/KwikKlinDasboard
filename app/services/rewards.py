"""Loyalty rewards — niyam Settings mein, inaam apne aap, coupon se lagta hai.

Dukaan ke niyam (RewardRule): "30 din mein 3 bill" ya "90 din mein ₹3000 ka
kaam" -> agle bill par ₹100 / 10% chhoot. Har bill banne par grahak ka hisaab
dekha jaata hai; niyam poora hua to uske naam ek single-use coupon banta hai
(Coupon.customer_id = wahi grahak, koi aur use nahi kar sakta), CustomerReward
mein record, aur grahak ko WhatsApp par khabar.

Bill banate waqt staff/dashboard ko "🎁 reward hai" dikhta hai aur ek tap
mein lagta hai (coupon wala hi rasta). Owner Settings se niyam band kar
sakta hai aur mila hua reward cancel bhi (coupon band ho jaata hai).

Suraksha:
- Sab tables TenantScoped + RLS: doosri dukaan ka niyam/reward kabhi nahi dikhta.
- Grahak ke bill page par sirf USKA progress/reward (token grahak ka hai);
  niyam ka naam/inaam bas utna jitna use samajhna hai.
- Coupon code grahak-locked (validate_coupon check karta hai).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Coupon, Customer, Order, OrderStatus
from app.models.rewards import CustomerReward, RewardRule

log = structlog.get_logger()

KINDS = ("bills", "spend")            # N bills in window | ₹X spend in window
REWARD_TYPES = ("flat", "percent")

# Laundry mein aam chalan: chhota, saaf, jaldi milne wala inaam. Owner ek tap
# mein jod sakta hai (Settings -> Rewards -> Add suggested), phir badal le.
SUGGESTED_RULES = [
    {"name": "3 bills in a month", "kind": "bills", "window_days": 30, "threshold": 3,
     "reward_type": "flat", "value": 100, "max_discount": None, "min_order": 300, "valid_days": 30},
    {"name": "₹3,000 in 3 months", "kind": "spend", "window_days": 90, "threshold": 3000,
     "reward_type": "percent", "value": 10, "max_discount": 300, "min_order": 200, "valid_days": 45},
]


def _money(v) -> str:
    d = Decimal(str(v or 0))
    return f"₹{d:,.0f}" if d == d.to_integral_value() else f"₹{d:,.2f}"


def reward_label(rule_or_row) -> str:
    """"₹100 off" / "10% off (max ₹300)" — jahan bhi inaam likha jaye, yahi."""
    rt, val, cap = rule_or_row.reward_type, rule_or_row.value, rule_or_row.max_discount
    if rt == "percent":
        return f"{Decimal(str(val)):.0f}% off" + (f" (max {_money(cap)})" if cap else "")
    return f"{_money(val)} off"


def condition_label(rule) -> str:
    if rule.kind == "bills":
        return f"{int(rule.threshold)} bills in {rule.window_days} days"
    return f"{_money(rule.threshold)} spent in {rule.window_days} days"


def rule_dict(r: RewardRule) -> dict:
    return {
        "id": str(r.id), "name": r.name, "kind": r.kind, "window_days": r.window_days,
        "threshold": float(r.threshold), "reward_type": r.reward_type, "value": float(r.value),
        "max_discount": float(r.max_discount) if r.max_discount is not None else None,
        "min_order": float(r.min_order) if r.min_order is not None else None,
        "valid_days": r.valid_days, "active": r.active,
        "reward": reward_label(r), "condition": condition_label(r),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def reward_dict(cr: CustomerReward, rule: RewardRule | None, cust: Customer | None = None) -> dict:
    return {
        "id": str(cr.id), "code": cr.coupon_code, "status": cr.status,
        "reward": cr.label, "rule": rule.name if rule else "",
        "earned_at": cr.earned_at.isoformat() if cr.earned_at else None,
        "expires_at": cr.expires_at.isoformat() if cr.expires_at else None,
        "customer": (cust.name or cust.phone) if cust else None,
        "customer_phone": cust.phone if cust else None,
        "note": cr.note,
    }


# ------------------------------------------------------------------ rules --

async def active_rules(db: AsyncSession) -> list[RewardRule]:
    return (
        await db.execute(select(RewardRule).where(RewardRule.active.is_(True)).order_by(RewardRule.created_at))
    ).scalars().all()


def _window_start(rule: RewardRule) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=int(rule.window_days))


async def _window_stats(db: AsyncSession, customer_id, rule: RewardRule) -> tuple[int, Decimal]:
    """Window mein grahak ke (cancelled chhod kar, daam wale) bill: ginti, rakam."""
    row = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(Order.total_amount), 0)).where(
                Order.customer_id == customer_id,
                Order.status != OrderStatus.CANCELLED,
                Order.total_amount.isnot(None),
                Order.created_at >= _window_start(rule),
            )
        )
    ).one()
    return int(row[0]), Decimal(str(row[1] or 0))


async def progress(db: AsyncSession, customer_id) -> list[dict]:
    """Grahak ke bill page ke liye: har chalu niyam par kitna ho gaya, kitna baaki."""
    out = []
    for rule in await active_rules(db):
        n, amt = await _window_stats(db, customer_id, rule)
        have = Decimal(n) if rule.kind == "bills" else amt
        need = Decimal(str(rule.threshold))
        out.append({
            "name": rule.name, "reward": reward_label(rule), "condition": condition_label(rule),
            "kind": rule.kind, "have": float(have), "need": float(need),
            "pct": int(min(100, (have / need * 100) if need else 100)),
            "done": have >= need,
        })
    return out


# ------------------------------------------------------------------ earn ----

def _code() -> str:
    # RW- + 6 akshar, bina 0/O/1/I ki uljhan — phone par bolne mein aasaan
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "RW-" + "".join(secrets.choice(alphabet) for _ in range(6))


async def evaluate(db: AsyncSession, customer: Customer, *, trigger_order: Order | None = None) -> list[CustomerReward]:
    """Har chalu niyam dekho; jo poora hua aur is window mein pehle nahi mila,
    uska coupon + record banao. Commit karta hai. Kabhi raise nahi karta."""
    earned: list[CustomerReward] = []
    try:
        for rule in await active_rules(db):
            n, amt = await _window_stats(db, customer.id, rule)
            have = Decimal(n) if rule.kind == "bills" else amt
            if have < Decimal(str(rule.threshold)):
                continue
            # Ek window mein ek hi baar — warna 4th, 5th bill par phir se milta
            already = (
                await db.execute(
                    select(func.count()).select_from(CustomerReward).where(
                        CustomerReward.customer_id == customer.id,
                        CustomerReward.rule_id == rule.id,
                        CustomerReward.earned_at >= _window_start(rule),
                    )
                )
            ).scalar_one()
            if already:
                continue
            code = _code()
            valid_to = date.today() + timedelta(days=int(rule.valid_days or 30))
            db.add(Coupon(
                code=code, discount_type=rule.reward_type, value=rule.value,
                min_order=rule.min_order, valid_from=date.today(), valid_to=valid_to,
                per_customer_limit=1, total_limit=1, customer_id=customer.id, active=True,
            ))
            cr = CustomerReward(
                customer_id=customer.id, rule_id=rule.id, coupon_code=code, status="earned",
                label=reward_label(rule),
                expires_at=datetime.combine(valid_to, datetime.max.time()).replace(tzinfo=timezone.utc),
                trigger_order_id=trigger_order.id if trigger_order is not None else None,
            )
            db.add(cr)
            await db.commit()
            earned.append(cr)
            log.info("reward_earned", customer=str(customer.id), rule=rule.name, code=code)
            await _notify(db, customer, cr, rule)
    except Exception:
        await db.rollback()
        log.exception("reward_evaluate_failed", customer=str(getattr(customer, "id", "")))
    return earned


async def _notify(db: AsyncSession, customer: Customer, cr: CustomerReward, rule: RewardRule) -> None:
    """Grahak ko WhatsApp: "🎁 aapko ₹100 off mila, code RW-XXXX, 30 Sep tak".
    Best-effort; API na judi ho to sirf log."""
    try:
        if customer.opted_out or not customer.is_active:
            return
        from app.models.tenant import Tenant
        from app.services import whatsapp as _wa
        from app.services.messages import get_message

        tenant = await db.get(Tenant, customer.tenant_id) if customer.tenant_id else None
        shop = (tenant.shop_name if tenant and tenant.shop_name else "").strip() or "Kwik Klin"
        text = get_message(
            "reward_earned", name=(customer.name or "").strip() or "there", shop=shop,
            reward=cr.label, code=cr.coupon_code,
            valid_to=cr.expires_at.strftime("%d %b") if cr.expires_at else "",
            condition=condition_label(rule),
        )
        try:
            await _wa.send_message(db, to_phone=customer.phone, text=text)
        except _wa.SendError as exc:
            log.info("reward_notify_skipped", reason=str(exc)[:80])
    except Exception:
        log.exception("reward_notify_failed")


async def on_order_created(db: AsyncSession, order: Order, customer: Customer, coupon: Coupon | None) -> None:
    """order_service.create_order ke baad: laga hua reward 'used' karo, phir naye
    inaam ka hisaab. Order ban chuka hai — yahan ki koi galti use nahi rokti."""
    try:
        if coupon is not None:
            await mark_used(db, coupon.code, order)
        await evaluate(db, customer, trigger_order=order)
    except Exception:
        await db.rollback()
        log.exception("reward_hook_failed", order=order.order_number)


async def mark_used(db: AsyncSession, code: str, order: Order) -> None:
    cr = (
        await db.execute(select(CustomerReward).where(CustomerReward.coupon_code == code.upper()))
    ).scalar_one_or_none()
    if cr is not None and cr.status == "earned":
        cr.status = "used"
        cr.used_order_id = order.id
        cr.used_at = datetime.now(timezone.utc)
        await db.commit()


# ------------------------------------------------------------------ read ----

async def available(db: AsyncSession, customer_id) -> list[dict]:
    """Bill banate waqt: is grahak ke jo reward abhi lag sakte hain."""
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(CustomerReward, RewardRule)
            .outerjoin(RewardRule, RewardRule.id == CustomerReward.rule_id)
            .where(
                CustomerReward.customer_id == customer_id,
                CustomerReward.status == "earned",
                CustomerReward.expires_at >= now,
            )
            .order_by(CustomerReward.earned_at)
        )
    ).all()
    return [reward_dict(cr, rule) for cr, rule in rows]


async def for_bill_page(db: AsyncSession, customer_id) -> dict:
    """Grahak ke apne page ke liye — sirf uska: mile hue reward + progress."""
    return {"available": await available(db, customer_id), "progress": await progress(db, customer_id)}


async def cancel(db: AsyncSession, reward_id: uuid.UUID, *, by: str, note: str = "") -> CustomerReward | None:
    """Owner ne reward hataya: coupon band, record cancelled (audit ke liye rehta hai)."""
    cr = await db.get(CustomerReward, reward_id)
    if cr is None or cr.status != "earned":
        return cr
    coupon = (await db.execute(select(Coupon).where(Coupon.code == cr.coupon_code))).scalar_one_or_none()
    if coupon is not None:
        coupon.active = False
    cr.status = "cancelled"
    cr.note = (note or "").strip()[:200] or f"cancelled by {by}"
    await db.commit()
    log.info("reward_cancelled", code=cr.coupon_code, by=by)
    return cr


async def expire_sweep(db: AsyncSession) -> int:
    """Nightly: jo reward ki tareekh nikal gayi, unhe 'expired' likh do."""
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(CustomerReward).where(CustomerReward.status == "earned", CustomerReward.expires_at < now)
        )
    ).scalars().all()
    for cr in rows:
        cr.status = "expired"
    if rows:
        await db.commit()
    return len(rows)
