from app.utils.dates import today_ist
"""⚡ Urgent kapde — extra charge aur jaldi delivery, ek hi jagah ka hisaab.

Charge bill ki ek alag line banta hai (kind="urgent_charge"), taaki:
  - total mein apne aap jude aur receipt par saaf "Urgent charge ₹X" dikhe
  - washerman ke work order / kapdon ki ginti mein na aaye (wo kapda nahi hai)
  - koi migration na lage — items JSON mein pehle se jagah hai

Default owner ki setting se (items ka % ya fixed ₹). Har bill par badla ja
sakta hai; 0 = maaf (line banti hi nahi, par order urgent rehta hai).
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import app_settings

KIND = "urgent_charge"
LABEL = "Urgent charge"


def is_charge_line(it) -> bool:
    """Paise ki line hai, kapda nahi — ginti, work order, delivery sab ise chhodte hain."""
    return isinstance(it, dict) and it.get("kind") == KIND


async def config(db: AsyncSession) -> dict:
    t = await app_settings.get(db, "urgent_charge_type")
    try:
        value = float(await app_settings.get(db, "urgent_charge_value") or 0)
    except (TypeError, ValueError):
        value = 0.0
    try:
        days = int(await app_settings.get(db, "urgent_delivery_days"))
    except (TypeError, ValueError):
        days = 1
    return {
        "type": "flat" if t == "flat" else "percent",
        "value": max(0.0, value),
        "days": max(0, min(days, 14)),
    }


def default_charge(cfg: dict, items_gross: Decimal) -> Decimal:
    """Items ke subtotal par default urgent charge, poore rupaye mein."""
    v = Decimal(str(cfg.get("value") or 0))
    raw = v if cfg.get("type") == "flat" else (items_gross * v / Decimal("100"))
    return raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP) if raw > 0 else Decimal("0")


def line(amount: Decimal) -> dict:
    a = float(amount)
    return {"type": LABEL, "service": "Urgent", "qty": 1, "rate": a, "amount": a,
            "unit": "pc", "kind": KIND}


def delivery_date(cfg: dict) -> date:
    return today_ist() + timedelta(days=int(cfg.get("days", 1)))
