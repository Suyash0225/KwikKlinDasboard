"""Create and open short-lived payment offers safely on the server."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, PaymentOffer

DURATION_SECONDS = 120
DISCOUNT_RUPEES = Decimal("30.00")


async def create_offer(
    db: AsyncSession,
    *,
    order: Order,
    kind: str,
    created_by: str,
) -> PaymentOffer | None:
    if kind not in {"advance", "reminder"}:
        raise ValueError("Unknown payment offer")
    due = (Decimal(str(order.total_amount or 0)) - Decimal(str(order.amount_paid or 0))).quantize(Decimal("0.01"))
    if due <= DISCOUNT_RUPEES:
        return None

    # A newly sent offer supersedes any older unopened offer for this bill.
    await db.execute(
        update(PaymentOffer)
        .where(
            PaymentOffer.order_id == order.id,
            PaymentOffer.opened_at.is_(None),
            PaymentOffer.expires_at.is_(None),
        )
        .values(expires_at=datetime.now(timezone.utc))
    )
    offer = PaymentOffer(
        tenant_id=order.tenant_id,
        order_id=order.id,
        kind=kind,
        original_amount=due,
        discount_amount=DISCOUNT_RUPEES,
        offer_amount=(due - DISCOUNT_RUPEES).quantize(Decimal("0.01")),
        duration_seconds=DURATION_SECONDS,
        created_by=created_by[:80] or "staff",
    )
    db.add(offer)
    await db.flush()
    return offer


async def open_offer(db: AsyncSession, offer: PaymentOffer, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if offer.opened_at is None and offer.expires_at is not None:
        return {
            "active": False,
            "expired": True,
            "remaining_seconds": 0,
            "original_amount": float(offer.original_amount),
            "discount_amount": float(offer.discount_amount),
            "offer_amount": float(offer.offer_amount),
            "kind": offer.kind,
            "expires_at": offer.expires_at.isoformat(),
        }
    if offer.opened_at is None:
        offer.opened_at = now
        offer.expires_at = now + timedelta(seconds=offer.duration_seconds)
        await db.flush()

    remaining = 0
    active = False
    if offer.expires_at is not None:
        remaining = max(0, int((offer.expires_at - now).total_seconds()))
        active = remaining > 0

    return {
        "active": active,
        "expired": not active,
        "remaining_seconds": remaining,
        "original_amount": float(offer.original_amount),
        "discount_amount": float(offer.discount_amount),
        "offer_amount": float(offer.offer_amount),
        "kind": offer.kind,
        "expires_at": offer.expires_at.isoformat() if offer.expires_at else None,
    }
