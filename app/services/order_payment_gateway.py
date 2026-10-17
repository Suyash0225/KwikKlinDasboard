"""Razorpay payment-link integration for customer order bills.

SaaS subscription billing remains in app.services.billing. This module is
only for the laundry's customer order payments.
"""
from __future__ import annotations

from decimal import Decimal
import uuid

import httpx
import structlog
from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import Customer, Order, Payment, PaymentMethod
from app.services import order_service, tenant_context

log = structlog.get_logger(__name__)
_API = "https://api.razorpay.com/v1"


class PaymentGatewayDisabled(RuntimeError):
    pass


class PaymentGatewayError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(
        settings.RAZORPAY_KEY_ID
        and settings.RAZORPAY_KEY_SECRET
        and settings.RAZORPAY_WEBHOOK_SECRET
    )


async def create_payment_link(db, order: Order, due: Decimal) -> str:
    """Create or reuse a hosted payment link for the current outstanding balance."""
    if not enabled():
        raise PaymentGatewayDisabled("Razorpay order payments are not configured")
    due = Decimal(str(due)).quantize(Decimal("0.01"))
    if due <= 0:
        raise PaymentGatewayError("outstanding amount must be positive")
    if (
        order.razorpay_payment_link_url
        and order.razorpay_payment_link_amount is not None
        and Decimal(str(order.razorpay_payment_link_amount)).quantize(Decimal("0.01")) == due
    ):
        return order.razorpay_payment_link_url

    reference_id = f"{order.order_number[:24]}-{uuid.uuid4().hex[:10]}"
    payload = {
        "amount": int(due * 100),
        "currency": "INR",
        "accept_partial": False,
        "description": f"Kwik Klin bill {order.order_number}"[:255],
        "reference_id": reference_id,
        "reminder_enable": False,
        "notify": {"sms": False, "email": False},
        "notes": {
            "tenant_id": str(order.tenant_id),
            "order_id": str(order.id),
            "order_number": order.order_number,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{_API}/payment_links",
                auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
                json=payload,
            )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("razorpay_order_link_api_failed", order=order.order_number, error=str(exc)[:180])
        raise PaymentGatewayError("Razorpay payment-link creation failed") from exc

    link_id, short_url = data.get("id"), data.get("short_url")
    if not isinstance(link_id, str) or not link_id.startswith("plink_") or not isinstance(short_url, str) or not short_url.startswith("https://"):
        raise PaymentGatewayError("Razorpay returned an invalid payment link")
    order.razorpay_payment_link_id = link_id
    order.razorpay_payment_link_url = short_url
    order.razorpay_payment_link_amount = due
    await db.commit()
    return short_url


async def handle_payment_link_paid(event: dict) -> dict:
    """Reconcile a signed payment_link.paid event exactly once.

    If the amount no longer matches the current balance (e.g. the owner also
    recorded a cash payment after the link was created), leave the ledger
    unchanged and raise an owner escalation for manual reconciliation.
    """
    entity = (((event.get("payload") or {}).get("payment_link") or {}).get("entity") or {})
    if entity.get("status") != "paid":
        return {"status": "ignored", "reason": "payment link is not paid"}
    link_id = entity.get("id")
    notes = entity.get("notes") or {}
    try:
        tenant_id = uuid.UUID(str(notes["tenant_id"]))
        order_id = uuid.UUID(str(notes["order_id"]))
        amount_paise = entity.get("amount_paid")
        if amount_paise is None:
            amount_paise = entity["amount"]
        amount = (Decimal(str(amount_paise)) / Decimal("100")).quantize(Decimal("0.01"))
    except (KeyError, ValueError, TypeError, ArithmeticError):
        log.warning("razorpay_order_payment_payload_invalid")
        return {"status": "ignored", "reason": "missing order reference or amount"}
    if not isinstance(link_id, str) or not link_id.startswith("plink_") or amount <= 0:
        return {"status": "ignored", "reason": "invalid payment-link id or amount"}

    async with tenant_context.as_tenant(tenant_id):
        async with async_session_factory() as db:
            existing = (
                await db.execute(select(Payment).where(Payment.provider_reference == link_id))
            ).scalar_one_or_none()
            if existing is not None:
                return {"status": "duplicate", "payment_id": str(existing.id)}

            order = (
                await db.execute(
                    select(Order).where(Order.id == order_id, Order.tenant_id == tenant_id).with_for_update()
                )
            ).scalar_one_or_none()
            if order is None:
                return {"status": "ignored", "reason": "order not found"}
            due = (Decimal(str(order.total_amount or 0)) - Decimal(str(order.amount_paid or 0))).quantize(Decimal("0.01"))
            if due != amount:
                customer = await db.get(Customer, order.customer_id)
                from app.services.escalation import raise_escalation

                await raise_escalation(
                    db,
                    customer=customer,
                    order_id=order.id,
                    question=(
                        f"Razorpay payment link {link_id} paid ₹{amount}, but "
                        f"order {order.order_number} currently has ₹{due} outstanding. "
                        "Payment was not auto-posted; reconcile the payment ledger."
                    ),
                )
                log.error(
                    "razorpay_order_payment_amount_mismatch",
                    order=order.order_number,
                    link_id=link_id,
                    received=str(amount),
                    due=str(due),
                )
                return {"status": "manual_review", "reason": "amount differs from current balance"}

            await order_service.record_payment(
                db,
                order,
                amount=amount,
                method=PaymentMethod.UPI,
                recorded_by="razorpay",
                note=f"Razorpay payment link {link_id}",
                provider_reference=link_id,
                notify_customer=True,
            )
            return {"status": "recorded", "order_number": order.order_number, "amount": str(amount)}
