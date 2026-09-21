"""Agentic customer tools: scoped reads + safe business actions.

The model may choose and chain these tools, but every execution is bound to the
authenticated Customer object and validated in Python. There is no raw SQL tool.
"""

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer, Order, OrderStatus, Staff
from app.services.ai_tools import (
    get_customer_bills,
    get_customer_orders,
    get_customer_profile,
    get_shop_rate_card,
)
from app.services.escalation import raise_escalation


AGENT_TOOLS = {
    "get_customer_profile": "Read the authenticated customer's name, phone and address.",
    "get_customer_orders": "Search the authenticated customer's orders from the last 3 months.",
    "get_order_details": "Get one of the authenticated customer's orders by order number.",
    "get_customer_bills": "Get the authenticated customer's unpaid bills and exact payment links.",
    "get_shop_rate_card": "Read the shop's current public prices.",
    "request_pickup": "Arrange a pickup for the authenticated customer. Use an existing order when possible; otherwise create a pickup order when clothing details are known.",
    "send_bill": "Send the authenticated customer's real bill/payment link.",
    "record_customer_issue": "Record a customer issue and alert the admin without exposing internal data.",
}


async def _get_order(db: AsyncSession, customer: Customer, order_number: str | None) -> Order | None:
    number = (order_number or "").strip().upper().replace(" ", "-")
    if not number:
        return None
    return (
        await db.execute(
            select(Order).where(
                Order.customer_id == customer.id,
                Order.order_number == number,
            )
        )
    ).scalar_one_or_none()


async def get_order_details(
    db: AsyncSession, customer: Customer, *, order_number: str
) -> dict[str, Any]:
    from app.services.ai_tools import get_customer_orders

    order = await _get_order(db, customer, order_number)
    if order is None:
        return {"ok": False, "error": "order_not_found"}
    rows = await get_customer_orders(db, customer, limit=20)
    for row in rows:
        if row["order_number"] == order.order_number:
            return {"ok": True, "order": row}
    return {"ok": False, "error": "order_outside_recent_window"}


async def request_pickup(
    db: AsyncSession,
    customer: Customer,
    *,
    pickup_date: str,
    order_number: str | None = None,
    items_text: str | None = None,
) -> dict[str, Any]:
    """Arrange a pickup using existing domain services; never accepts a staff id."""
    try:
        pickup_day = date.fromisoformat((pickup_date or "").strip())
    except ValueError:
        return {"ok": False, "needs_input": True, "error": "pickup_date_must_be_YYYY-MM-DD"}

    if pickup_day < date.today():
        return {"ok": False, "needs_input": True, "error": "pickup_date_is_in_the_past"}

    from app.services import app_settings
    from app.services.order_service import create_order, update_status
    from app.services.tasks import create_pickup_task

    order = await _get_order(db, customer, order_number)
    if order is None and not order_number:
        # If the customer already has an open order, use it rather than making
        # a duplicate. Otherwise the model must provide clothing details.
        rows = (
            await db.execute(
                select(Order)
                .where(
                    Order.customer_id == customer.id,
                    Order.status.in_(
                        (OrderStatus.RECEIVED, OrderStatus.PICKUP_ASSIGNED)
                    ),
                )
                .order_by(Order.created_at.desc())
                .limit(1)
            )
        ).scalars().all()
        order = rows[0] if rows else None

    if order is None:
        items = (items_text or "").strip()
        if not items:
            return {
                "ok": False,
                "needs_input": True,
                "missing": "items_text",
                "message": "Ask the customer what clothes/items should be picked up.",
            }
        if not (customer.name or "").strip() or not (customer.address or "").strip():
            return {
                "ok": False,
                "needs_input": True,
                "missing": "customer_profile",
                "message": "Ask only for the missing customer name/address before creating a new pickup order.",
            }
        order = await create_order(
            db,
            customer_phone=customer.phone,
            customer_name=customer.name,
            items=[{"type": items[:100], "qty": 1}],
            pickup_date=pickup_day,
            needs_pickup=True,
            notes=f"AI pickup request: {pickup_day.isoformat()}",
            created_by="customer-ai",
        )
        return {
            "ok": True,
            "created": True,
            "order_number": order.order_number,
            "pickup_date": pickup_day.isoformat(),
            "message": "Pickup order created; the existing ops workflow assigned pickup staff and notified the customer/admin.",
        }

    if order.status in {OrderStatus.DELIVERED, OrderStatus.CANCELLED}:
        return {"ok": False, "needs_input": True, "error": "order_is_not_pickup_eligible"}

    if order.status is OrderStatus.RECEIVED:
        await update_status(
            db, order, OrderStatus.PICKUP_ASSIGNED, changed_by="customer-ai", notify=False
        )
    task = await create_pickup_task(db, order)
    if task is None:
        return {
            "ok": False,
            "error": "no_delivery_staff_available",
            "message": "Pickup request saved but no delivery staff could be assigned yet.",
        }

    await db.refresh(order)
    return {
        "ok": True,
        "created": False,
        "order_number": order.order_number,
        "pickup_date": pickup_day.isoformat(),
        "task_code": task.code,
        "assigned_staff_id": str(order.assigned_delivery_id) if order.assigned_delivery_id else None,
        "message": "Pickup task created through the normal task/ops workflow.",
    }


async def send_bill(
    db: AsyncSession,
    customer: Customer,
    *,
    order_number: str | None = None,
) -> dict[str, Any]:
    from app.services.order_service import send_bill_to_customer

    order = await _get_order(db, customer, order_number) if order_number else None
    sent = await send_bill_to_customer(
        db, customer, order_number=order.order_number if order else None
    )
    return {"ok": bool(sent), "order_number": order.order_number if order else None}


async def record_customer_issue(
    db: AsyncSession,
    customer: Customer,
    *,
    issue: str,
    order_number: str | None = None,
) -> dict[str, Any]:
    order = await _get_order(db, customer, order_number) if order_number else None
    question = f"Customer issue"
    if order is not None:
        question += f" [{order.order_number}]"
    question += f": {(issue or '').strip()[:1500]}"
    esc = await raise_escalation(
        db, question=question, customer=customer,
        order_id=order.id if order else None,
    )
    return {"ok": esc is not None, "escalation_id": str(esc.id) if esc else None}


async def run_agent_tool(
    db: AsyncSession,
    customer: Customer,
    name: str,
    args: dict[str, Any] | None = None,
) -> Any:
    args = args or {}
    if name == "get_customer_profile":
        return await get_customer_profile(db, customer)
    if name == "get_customer_orders":
        limit = max(1, min(int(args.get("limit", 20) or 20), 20))
        return await get_customer_orders(db, customer, limit=limit)
    if name == "get_order_details":
        return await get_order_details(
            db, customer, order_number=str(args.get("order_number") or "")
        )
    if name == "get_customer_bills":
        limit = max(1, min(int(args.get("limit", 20) or 20), 20))
        return await get_customer_bills(db, customer, limit=limit)
    if name == "get_shop_rate_card":
        return await get_shop_rate_card(db, customer)
    if name == "request_pickup":
        return await request_pickup(
            db, customer,
            pickup_date=str(args.get("pickup_date") or ""),
            order_number=(str(args["order_number"]) if args.get("order_number") else None),
            items_text=(str(args["items_text"]) if args.get("items_text") else None),
        )
    if name == "send_bill":
        return await send_bill(
            db, customer,
            order_number=(str(args["order_number"]) if args.get("order_number") else None),
        )
    if name == "record_customer_issue":
        return await record_customer_issue(
            db, customer,
            issue=str(args.get("issue") or ""),
            order_number=(str(args["order_number"]) if args.get("order_number") else None),
        )
    raise ValueError(f"unknown customer agent tool: {name}")
