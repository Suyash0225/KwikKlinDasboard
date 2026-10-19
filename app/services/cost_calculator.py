"""Deterministic owner-facing operating-cost metrics.

This is an estimate, not audited unit costing: Expense rows currently have no
allocation basis and historical order lines may lack a reliable weight. The
calculator therefore exposes the raw expense pool, completed volumes, and
clearly-labelled ratios without pretending shared costs are allocated correctly.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Expense, Order, OrderStatus, Rate

IST = timezone(timedelta(hours=5, minutes=30))
ZERO = Decimal("0")


def _unit_key(value: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _period_start(period: str, now: datetime | None = None):
    current = (now or datetime.now(IST)).astimezone(IST)
    value = (period or "month").strip().lower()
    if value in {"all", "all-time", "lifetime"}:
        return None, "all-time"
    if value in {"today", "aaj"}:
        return current.replace(hour=0, minute=0, second=0, microsecond=0), "today"
    if value in {"week", "7d", "7 din"}:
        return current - timedelta(days=7), "last 7 days"
    return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "this month"


def _decimal(value) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO
    return result if result.is_finite() and result >= 0 else ZERO


def summarize_costs(expenses, order_rows, rates, period_label: str) -> dict:
    """Pure calculation; caller supplies rows already scoped by tenant/date."""
    expense_total = sum((_decimal(e) for e in expenses), ZERO)
    rate_units = {}
    for rate in rates:
        key = (
            _unit_key(str(getattr(rate, "service", "") or "")),
            _unit_key(str(getattr(rate, "garment", "") or "")),
        )
        rate_units[key] = str(getattr(rate, "unit", "pc") or "pc").lower()

    pieces = ZERO
    kilograms = ZERO
    unclassified = ZERO
    completed_orders = 0
    for row in order_rows:
        order = row[0] if isinstance(row, tuple) else row
        if getattr(order, "status", None) != OrderStatus.DELIVERED:
            continue
        completed_orders += 1
        for item in (getattr(order, "items", None) or []):
            if not isinstance(item, dict) or item.get("kind") in {"extra_charge", "due_charge"}:
                continue
            raw_qty = item.get("qty")
            if raw_qty is None:
                unclassified += 1
                continue
            qty = _decimal(raw_qty)
            if qty <= 0:
                unclassified += 1
                continue
            service = _unit_key(str(item.get("service") or ""))
            explicit_garment = _unit_key(str(item.get("garment") or ""))
            type_key = _unit_key(str(item.get("type") or ""))
            explicit_unit = str(item.get("unit") or "").lower()
            unit = explicit_unit if explicit_unit in {"pc", "kg"} else (
                rate_units.get((service, explicit_garment))
                or rate_units.get((service, ""))
                or rate_units.get((service, type_key))
            )
            if unit == "kg":
                kilograms += qty
            elif unit == "pc":
                pieces += qty
            else:
                unclassified += 1

    mixed_units = pieces > 0 and kilograms > 0
    # The same shared expense pool cannot safely be divided independently by
    # pieces and kilograms. Suppress both ratios for mixed-unit periods rather
    # than presenting two misleading unit-cost figures.
    can_show_unit_proxy = bool(expenses) and not mixed_units
    cost_per_piece = (
        (expense_total / pieces).quantize(Decimal("0.01"))
        if can_show_unit_proxy and pieces > 0 else None
    )
    cost_per_kg = (
        (expense_total / kilograms).quantize(Decimal("0.01"))
        if can_show_unit_proxy and kilograms > 0 else None
    )
    if not expenses:
        proxy_note = "No expenses recorded; unit cost is unavailable, not zero."
    elif mixed_units:
        proxy_note = (
            "Mixed piece and kg volumes share one expense pool; unit-cost proxies "
            "are suppressed until expenses are allocated by unit/service."
        )
    elif pieces > 0 or kilograms > 0:
        proxy_note = (
            "Shared-expense proxy only; expenses are not allocated by service "
            "and this is not an audited unit cost or margin."
        )
    else:
        proxy_note = "No classifiable delivered volume; unit-cost proxy is unavailable."

    return {
        "period": period_label,
        "expense_total": expense_total,
        "completed_orders": completed_orders,
        "pieces": pieces,
        "kilograms": kilograms,
        "unclassified_lines": int(unclassified),
        "cost_per_piece_proxy": cost_per_piece,
        "cost_per_kg_proxy": cost_per_kg,
        "mixed_units": mixed_units,
        "expense_rows": len(expenses),
        "has_expenses": bool(expenses),
        "cost_proxy_note": proxy_note,
    }


async def calculate_cost_metrics(db: AsyncSession, period: str = "month") -> dict:
    """Read tenant-scoped expense/order/rate data and calculate reproducible metrics."""
    start_ist, label = _period_start(period)
    expense_query = select(Expense.amount)
    order_query = select(Order).where(Order.status == OrderStatus.DELIVERED)
    if start_ist is not None:
        start_utc = start_ist.astimezone(timezone.utc)
        expense_query = expense_query.where(Expense.spent_on >= start_ist.date())
        order_query = order_query.where(Order.created_at >= start_utc)
    expenses = (await db.execute(expense_query)).scalars().all()
    orders = (await db.execute(order_query.order_by(Order.created_at))).scalars().all()
    rates = (await db.execute(select(Rate))).scalars().all()
    return summarize_costs(expenses, orders, rates, label)
