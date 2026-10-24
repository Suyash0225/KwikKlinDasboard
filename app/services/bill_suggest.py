"""Bill banate waqt sahi item jaldi mile — do ishaare, dono purane bills se.

1. Dukaan mein kya sabse zyada bikta hai (`shop_counts`): rate card ki har
   (service, garment) jodi pichhle 90 din mein KITNE BILLON mein aayi. Bill
   gine jaate hain, pieces nahi — ek grahak ke 20 mojey "Socks" ko dukaan ka
   sabse bada item nahi bana dete.
2. Ye grahak aam taur par kya deta hai (`customer_usual`): uske pichhle 20
   bills mein jo jodi baar-baar aayi, aur pichhli baar kitni qty thi.

Sirf SUJHAAV hai: bill mein apne aap kuch nahi judta. Har baar kapde alag
hote hain; khud bhara hua bill galti se aage chala jaye to galat paisa.

Koi nayi table nahi — `orders.items` (JSONB) hi sach hai. Tenant filter
ORM khud lagata hai (database.py), isliye doosri dukaan ke bill kabhi nahi
gine jaate.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderStatus, Rate
from app.services.item_savings import item_key, rate_key

SHOP_DAYS = 90
SHOP_MAX_ORDERS = 2000        # bahut badi dukaan par bhi ek request halki rahe
CUSTOMER_ORDERS = 20
USUAL_LIMIT = 6

# Bill ki asli kapde wali line nahi: urgent fee, baad mein joda charge, purana baaki
_NOT_GOODS = {"urgent_charge", "extra_charge", "due_charge"}


def _goods(items) -> list[dict]:
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict) and it.get("kind") not in _NOT_GOODS
            and (it.get("service") or it.get("type"))]


async def shop_counts(db: AsyncSession, *, days: int = SHOP_DAYS) -> Counter:
    """{(service, garment) lowercase: kitne bills mein} — pichhle `days` din."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await db.execute(
            select(Order.items)
            .where(Order.created_at >= since, Order.status != OrderStatus.CANCELLED)
            .order_by(Order.created_at.desc())
            .limit(SHOP_MAX_ORDERS)
        )
    ).scalars().all()
    counts: Counter = Counter()
    for items in rows:
        counts.update({item_key(it) for it in _goods(items)})    # set: ek bill = ek ginti
    return counts


async def customer_usual(db: AsyncSession, customer_id: UUID, *, limit: int = USUAL_LIMIT) -> list[dict]:
    """Is grahak ke aam items, sabse zyada baar wale pehle (barabar ho to
    jo haal mein aaya). Sirf wahi jo rate card mein abhi chalu hain — band
    ho chuki service ka sujhaav bill mein jud hi nahi sakta."""
    rows = (
        await db.execute(
            select(Order.items)
            .where(Order.customer_id == customer_id, Order.status != OrderStatus.CANCELLED)
            # ek hi pal ke do bill (ek transaction mein now() ek hi rehta hai)
            # — order_number din ke andar badhta hai, wahi kram tay kare
            .order_by(Order.created_at.desc(), Order.order_number.desc())
            .limit(CUSTOMER_ORDERS)
        )
    ).scalars().all()
    if not rows:
        return []
    rates = {
        rate_key(r.service, r.garment): r
        for r in (await db.execute(select(Rate).where(Rate.is_active.is_(True)))).scalars().all()
    }
    times: Counter = Counter()
    last_qty: dict[tuple[str, str], float] = {}
    recency: dict[tuple[str, str], int] = {}
    for age, items in enumerate(rows):                      # age 0 = sabse naya bill
        seen = set()
        for it in _goods(items):
            key = item_key(it)
            if key not in rates:
                continue
            if key not in seen:
                times[key] += 1
                seen.add(key)
            if key not in last_qty:                        # naye bill ki qty pehle milti hai
                try:
                    last_qty[key] = float(it.get("qty") or 1)
                except (TypeError, ValueError):
                    last_qty[key] = 1.0
                recency[key] = age
    ranked = sorted(times, key=lambda k: (-times[k], recency[k]))[:limit]
    out = []
    for key in ranked:
        r = rates[key]
        qty = last_qty[key]
        out.append({
            "service": r.service, "garment": r.garment, "unit": r.unit,
            "qty": int(qty) if qty == int(qty) else round(qty, 2),
            "times": times[key],
        })
    return out
