"""Conservative discount cap from owner-entered per-unit variable costs."""

from decimal import Decimal, InvalidOperation


def effective_discount_cap(configured_cap, rate_rows) -> tuple[float, bool]:
    """Return (cap, guarded); missing costs are unknown, never zero.

    If every active rate has a known cost, cap percent discounts at the smallest
    gross contribution percentage across the rate card. This excludes shared
    overhead and is not net profit. If any cost is missing, retain the configured
    cap but explicitly report that margin safety was not verified.
    """
    try:
        cap = max(0.0, float(configured_cap or 0))
    except (TypeError, ValueError):
        cap = 0.0
    rows = list(rate_rows or [])
    if not rows or any(row.get("cost_rate") is None for row in rows):
        return cap, False
    margins = []
    for row in rows:
        try:
            price = Decimal(str(row.get("rate")))
            cost = Decimal(str(row.get("cost_rate")))
        except (InvalidOperation, TypeError, ValueError):
            return cap, False
        if not price.is_finite() or not cost.is_finite() or price <= 0 or cost < 0:
            return cap, False
        margin = max(Decimal("0"), (price - cost) / price * Decimal("100"))
        margins.append(float(margin))
    return min(cap, min(margins)), True
