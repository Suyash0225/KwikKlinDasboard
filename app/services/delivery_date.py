"""Single source of truth for automatic customer delivery dates."""

from datetime import date, timedelta

HEAVY_KEYWORDS = (
    "blanket", "kambal", "razai", "quilt", "curtain", "parda",
    "carpet", "sofa", "saree", "lehenga", "sherwani",
)


def is_heavy_item(item_type: str) -> bool:
    value = (item_type or "").strip().lower()
    return any(word in value for word in HEAVY_KEYWORDS)


def calculate(
    start: date,
    items: list[dict] | None = None,
    *,
    normal_days: int = 4,
    heavy_days: int = 5,
    holidays: list[str] | tuple[str, ...] | None = None,
) -> date:
    """Return the promised date after working days.

    Sunday and configured holiday dates are not counted. Heavy-item orders
    get the heavy SLA; otherwise the normal SLA is used.
    """
    items = items or []
    days = max(1, int(heavy_days if any(
        is_heavy_item(str(item.get("type", ""))) for item in items
        if isinstance(item, dict)
    ) else normal_days))

    blocked: set[date] = set()
    for value in holidays or ():
        try:
            blocked.add(date.fromisoformat(str(value)))
        except (TypeError, ValueError):
            continue

    current = start
    counted = 0
    while counted < days:
        current += timedelta(days=1)
        if current.weekday() == 6 or current in blocked:
            continue
        counted += 1
    return current
