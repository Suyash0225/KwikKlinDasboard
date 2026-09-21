"""Customer-safe AI tool registry.

The LLM never receives a database session and never gets raw SQL access.
Every customer-facing read goes through a named tool with an explicit
contract and tenant/customer scope.

Phase 1 keeps the existing reply pipeline intact while moving business-data
access behind one tool boundary. Action tools continue to use
action_registry.py.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer, Order, OrderStatus, Rate
from app.services.messages import status_label


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., Awaitable[Any]]


async def get_customer_profile(db: AsyncSession, customer: Customer) -> dict[str, Any]:
    """Return only fields safe for the customer's own AI conversation."""
    return {
        "name": customer.name or None,
        "phone": customer.phone,
        "address": customer.address or None,
        "active": bool(customer.is_active),
        "opted_out": bool(customer.opted_out),
    }


async def get_customer_orders(
    db: AsyncSession,
    customer: Customer,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return this customer's recent orders with canonical status facts only."""
    limit = max(1, min(int(limit), 10))
    rows = list(
        (
            await db.execute(
                select(Order)
                .where(Order.customer_id == customer.id)
                .order_by(Order.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    )

    today = date.today()
    result: list[dict[str, Any]] = []
    for order in rows:
        expected = order.expected_delivery
        actual = order.actual_delivery

        # Canonical business truth: a delivered order is never represented
        # as having a future/expected delivery date.
        overdue = (
            order.status not in {OrderStatus.DELIVERED, OrderStatus.CANCELLED}
            and expected is not None
            and expected < today
        )

        result.append(
            {
                "order_number": order.order_number,
                "status": order.status.name,
                "status_label": status_label(order.status),
                "expected_delivery": (\n                    None\n                    if order.status is OrderStatus.DELIVERED\n                    else (expected.isoformat() if expected else None)\n                ),
                "actual_delivery": actual.isoformat() if actual else None,
                "overdue": overdue,
                "total_amount": str(order.total_amount) if order.total_amount is not None else None,
                "amount_paid": str(order.amount_paid or 0),
                "amount_due": (
                    str(max(order.total_amount - (order.amount_paid or 0), 0))
                    if order.total_amount is not None
                    else None
                ),
            }
        )
    return result


async def get_customer_bills(
    db: AsyncSession,
    customer: Customer,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return unpaid bill facts for this customer; no payment secrets."""
    orders = await get_customer_orders(db, customer, limit=limit)
    return [
        {
            "order_number": row["order_number"],
            "total_amount": row["total_amount"],
            "amount_paid": row["amount_paid"],
            "amount_due": row["amount_due"],
            "status": row["status"],
        }
        for row in orders
        if row["amount_due"] is not None and float(row["amount_due"]) > 0
    ]


async def get_shop_rate_card(
    db: AsyncSession,
    customer: Customer,
) -> list[dict[str, Any]]:
    """Return the active public rate card."""
    rates = (
        (
            await db.execute(
                select(Rate).where(Rate.is_active).order_by(Rate.service, Rate.garment)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "service": r.service,
            "garment": r.garment,
            "rate": str(r.rate),
            "unit": r.unit,
        }
        for r in rates
    ]


CUSTOMER_READ_TOOLS: dict[str, ToolSpec] = {
    "get_customer_profile": ToolSpec(
        "get_customer_profile",
        "Get the authenticated customer's own profile fields.",
        get_customer_profile,
    ),
    "get_customer_orders": ToolSpec(
        "get_customer_orders",
        "Get the authenticated customer's recent orders and canonical delivery status.",
        get_customer_orders,
    ),
    "get_customer_bills": ToolSpec(
        "get_customer_bills",
        "Get the authenticated customer's unpaid bill facts.",
        get_customer_bills,
    ),
    "get_shop_rate_card": ToolSpec(
        "get_shop_rate_card",
        "Get the shop's active public rate card.",
        get_shop_rate_card,
    ),
}


async def run_customer_tool(
    db: AsyncSession,
    customer: Customer,
    tool_name: str,
    **kwargs: Any,
) -> Any:
    """Single execution boundary for customer-facing read tools.

    Unknown tools fail closed. The caller never gets a generic SQL interface.
    """
    spec = CUSTOMER_READ_TOOLS.get(tool_name)
    if spec is None:
        raise ValueError(f"unknown customer AI tool: {tool_name}")
    return await spec.handler(db, customer, **kwargs)
