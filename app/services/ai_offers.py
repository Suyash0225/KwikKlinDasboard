"""AI ka apna offer — grahak ko chhoot ka coupon, par had code mein.

Model sirf itna bolta hai ki "is grahak ko X% do" (offer_percent). Coupon
yahan banta hai, aur har had Python mein, model ke haath mein nahi:

  - zyada se zyada 20%, aur sirf ₹1000+ ke order par (owner ka faisla);
  - coupon usi grahak ke naam (customer_id) — forward ho jaye to bhi kisi
    aur par nahi chalta; ek baar; 7 din;
  - har dukaan ka apna (Coupon tenant-scoped hai — RLS);
  - ek grahak ko 30 din mein ek hi naya offer. Chalta hua offer ho to
    wahi dobara dikhate hain, naya nahi banate — warna har "discount do"
    par naya coupon.

Redeem wahi purana raasta: counter/pickup bill par validate_coupon.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

MAX_PERCENT = 20
MIN_PERCENT = 5
MIN_ORDER = Decimal("1000")
VALID_DAYS = 7
COOLDOWN_DAYS = 30
PREFIX = "AI"


def _today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


def clamp(percent) -> int:
    """Model ka number -> 0 (offer nahi) ya MIN..MAX ke beech."""
    try:
        p = int(round(float(percent or 0)))
    except (TypeError, ValueError):
        return 0
    if p <= 0:
        return 0
    return max(MIN_PERCENT, min(MAX_PERCENT, p))


async def _latest(db: AsyncSession, customer):
    from app.models import Coupon

    return (
        await db.execute(
            select(Coupon)
            .where(Coupon.customer_id == customer.id, Coupon.code.like(f"{PREFIX}%"))
            .order_by(Coupon.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _used(db: AsyncSession, coupon) -> bool:
    from app.models import CouponRedemption

    n = (
        await db.execute(
            select(func.count()).select_from(CouponRedemption)
            .where(CouponRedemption.coupon_code == coupon.code)
        )
    ).scalar_one()
    return n > 0


async def status(db: AsyncSession, customer) -> dict:
    """FACTS ke liye: {"active": coupon|None, "can_offer": bool}."""
    last = await _latest(db, customer)
    today = _today()
    if last is not None and last.active and (last.valid_to is None or last.valid_to >= today) \
            and not await _used(db, last):
        return {"active": last, "can_offer": False}
    if last is not None and last.created_at is not None and \
            last.created_at >= datetime.now(timezone.utc) - timedelta(days=COOLDOWN_DAYS):
        return {"active": None, "can_offer": False}
    return {"active": None, "can_offer": True}


def facts_line(st: dict) -> str:
    c = st["active"]
    if c is not None:
        return (
            f"AI offer already given (do not create another): code {c.code}, {int(c.value)}% off "
            f"on orders ₹{int(c.min_order or MIN_ORDER)}+, valid till {c.valid_to}."
        )
    if st["can_offer"]:
        return (
            f"AI offer available: you MAY set offer_percent ({MIN_PERCENT}-{MAX_PERCENT}) to give this "
            f"customer a personal coupon, valid only on orders ₹{int(MIN_ORDER)}+."
        )
    return "AI offer: not available for this customer right now (offered recently). Do not promise a discount."


async def give(db: AsyncSession, customer, percent) -> object | None:
    """Coupon banao (ya chalta hua lautao). None = abhi offer nahi."""
    from app.models import Coupon
    from app.services import audit

    pct = clamp(percent)
    if not pct or customer is None or customer.id is None:
        return None
    st = await status(db, customer)
    if st["active"] is not None:
        return st["active"]
    if not st["can_offer"]:
        return None
    today = _today()
    for _ in range(8):
        code = PREFIX + secrets.token_hex(3).upper()
        exists = (
            await db.execute(select(func.count()).select_from(Coupon).where(Coupon.code == code))
        ).scalar_one()
        if exists:
            continue
        coupon = Coupon(
            code=code, discount_type="percent", value=Decimal(pct), min_order=MIN_ORDER,
            valid_from=today, valid_to=today + timedelta(days=VALID_DAYS),
            per_customer_limit=1, total_limit=1, customer_id=customer.id, active=True,
        )
        db.add(coupon)
        await db.commit()
        await audit.record(
            actor_role="system", actor="agent", action="ai_offer_created",
            args={"customer": customer.phone, "percent": pct, "min_order": int(MIN_ORDER)},
            result=code,
        )
        log.info("ai_offer_created", code=code, percent=pct)
        return coupon
    log.error("ai_offer_code_alloc_failed")
    return None


def offer_line(coupon, link: str = "") -> str:
    from app.services.messages import get_message

    return get_message(
        "ai_offer_line",
        percent=str(int(coupon.value)),
        min_order=str(int(coupon.min_order or MIN_ORDER)),
        code=coupon.code,
        valid_to=coupon.valid_to.strftime("%d %b") if coupon.valid_to else "",
        link_line=(f"\n{link}" if link else ""),
    )
