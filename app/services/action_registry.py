"""Deterministic action registry for AI decisions.

The model can select an action, but this module is the only execution boundary.
Handlers use database state and existing domain services; AI never supplies
money, URLs, staff IDs or campaign recipients.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer

ACTION_HANDLERS: dict[str, str] = {
    "NONE": "no-op",
    "ANSWER": "reply only",
    "SEND_BILL": "send the customer's real unpaid bill/payment link",
    "CREATE_LEAD": "capture a new enquiry in the lead pipeline",
    "FOLLOW_UP_LEAD": "handled by the lead scheduler/pipeline",
    "CREATE_ORDER": "handled by deterministic order-intake flow",
    "ESCALATE": "handled by deterministic escalation flow",
    "CREATE_CAMPAIGN": "handled by marketing agent/admin approval flow",
    "REFERRAL_REQUEST": "handled by marketing/customer engagement flow",
}


async def execute_customer_action(
    db: AsyncSession,
    customer: Customer,
    action: str,
    *,
    text: str = "",
) -> bool:
    """Execute only actions that are safe and fully deterministic here.

    Returns True when this registry handled the action.
    """
    if action == "SEND_BILL":
        from app.services.order_service import send_bill_to_customer

        return await send_bill_to_customer(db, customer)

    if action == "CREATE_LEAD":
        from app.services.leads import note_inquiry

        await note_inquiry(db, customer, text)
        return True

    # These actions deliberately stay in their existing domain workflows.
    # Returning False means the caller continues with the normal flow.
    return False
