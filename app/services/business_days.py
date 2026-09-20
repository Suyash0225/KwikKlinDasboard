"""Business-day helpers for delivery promises.

Sunday is always a non-working day. Shop-specific holidays come from the
hot-loaded delivery_holidays setting as ISO dates (YYYY-MM-DD).
"""

from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import app_settings


async def add_delivery_days(db: AsyncSession, start: date, days: int) -> date:
    """Return the date after days working days from start."""
    try:
        n = max(int(days), 0)
    except (TypeError, ValueError):
        n = 0

    raw = await app_settings.get(db, "delivery_holidays")
    holidays: set[date] = set()
    if isinstance(raw, (list, tuple, set)):
        for value in raw:
            try:
                holidays.add(date.fromisoformat(str(value)))
            except (TypeError, ValueError):
                continue

    current = start
    completed = 0
    while completed < n:
        current += timedelta(days=1)
        if current.weekday() == 6 or current in holidays:
            continue
        completed += 1
    return current
