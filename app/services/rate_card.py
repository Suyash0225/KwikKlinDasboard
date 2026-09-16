"""Rate card mein ek daam jodna — dashboard aur staff panel ka ek hi rasta."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Rate


class DuplicateRate(ValueError):
    pass


async def add_rate(
    db: AsyncSession, *, service: str, garment: str, unit: str, rate: Decimal
) -> tuple[Rate, bool]:
    """(row, revived). Pehle se chalu row ho to DuplicateRate.

    Band kiya hua row (Settings mein rate khali karne par) bill par dikhta
    nahi, par unique key use rok ke baitha rehta hai — New Bill se wahi
    item dobara jodne par "already on the rate card" aata tha. Use naye rate
    ke saath wapas chalu kar dete hain.
    """
    service, garment = service.strip(), garment.strip()
    old = (
        await db.execute(select(Rate).where(Rate.service == service, Rate.garment == garment))
    ).scalar_one_or_none()
    if old is not None:
        if old.is_active:
            raise DuplicateRate("This service + item is already on the rate card")
        old.rate, old.unit, old.is_active = rate, unit, True
        await db.commit()
        return old, True
    row = Rate(service=service, garment=garment, unit=unit, rate=rate)
    db.add(row)
    try:
        await db.commit()
    except Exception:
        # do log ek saath jodein to unique key yahin pakadti hai
        await db.rollback()
        raise DuplicateRate("This service + item is already on the rate card")
    return row, False
