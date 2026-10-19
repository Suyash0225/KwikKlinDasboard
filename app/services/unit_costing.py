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



def clamp_campaign_discount(
    offer_type, proposed_value, percent_cap, average_order_value, rate_rows,
    cost_guard_applied,
) -> float:
    """Clamp percent discounts as percentages and flat discounts as currency.

    Flat discounts are limited to the configured percentage of average order
    value. When every active rate has a known cost, they are further limited
    to the smallest per-unit gross contribution. This is deliberately
    conservative and is not a net-profit calculation.
    """
    try:
        value = max(0.0, float(proposed_value or 0))
        percent = max(0.0, float(percent_cap or 0))
        aov = max(0.0, float(average_order_value or 0))
    except (TypeError, ValueError):
        return 0.0

    kind = str(offer_type or "").strip().lower()
    if kind == "percent":
        return min(value, percent)
    if kind != "flat":
        return 0.0

    flat_cap = aov * percent / 100.0
    rows = list(rate_rows or [])
    if cost_guard_applied and rows:
        contributions = []
        for row in rows:
            try:
                price = Decimal(str(row.get("rate")))
                cost = Decimal(str(row.get("cost_rate")))
            except (InvalidOperation, TypeError, ValueError):
                return min(value, flat_cap)
            if not price.is_finite() or not cost.is_finite() or price <= 0 or cost < 0:
                return min(value, flat_cap)
            contributions.append(float(max(Decimal("0"), price - cost)))
        if contributions:
            flat_cap = min(flat_cap, min(contributions))
    return min(value, flat_cap)
