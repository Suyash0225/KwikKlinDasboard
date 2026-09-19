"""Public payment-offer endpoints plus staff/manager offer creation."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Customer, Order, PaymentOffer
from app.services import bill_link, payment_offers
from app.services.staff_auth import StaffPrincipal
from app.routers.staff_panel import require_biller
from app.routers.orders import require_admin_key

router = APIRouter(tags=["payment-offers"])


async def _order_for_bill(db: AsyncSession, token: str):
    parsed = bill_link.parse(token)
    if parsed is None:
        raise HTTPException(status_code=404, detail="Bill link is not valid")
    tid, oid = parsed
    from app.services import integrations
    async with integrations.tenant_db(tid) as scoped:
        order = await scoped.get(Order, oid)
        if order is None or order.tenant_id != tid:
            raise HTTPException(status_code=404, detail="Bill not found")
        return tid, oid


@router.get("/payment-offers/open", include_in_schema=False)
async def open_payment_offer(
    bill: str = Query(min_length=10),
    o: str | None = Query(default=None, min_length=1),
    offer: str | None = Query(default=None, min_length=1),
    db: AsyncSession = Depends(get_db),
):
    parsed = await bill_link.resolve(bill)
    if parsed is None:
        raise HTTPException(status_code=404, detail="Bill not found")
    tid, oid = parsed
    offer_key = (o or offer or "").strip()
    if not offer_key:
        raise HTTPException(status_code=404, detail="Offer not found")

    from app.services import integrations
    async with integrations.tenant_db(tid) as scoped:
        conditions = [
            PaymentOffer.order_id == oid,
            PaymentOffer.tenant_id == tid,
        ]
        try:
            offer_id = uuid.UUID(offer_key)
            conditions.append(PaymentOffer.id == offer_id)
        except ValueError:
            conditions.append(PaymentOffer.offer_code == offer_key.upper())
        row = (
            await scoped.execute(select(PaymentOffer).where(*conditions))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Offer not found")
        order = await scoped.get(Order, oid)
        if order is None:
            raise HTTPException(status_code=404, detail="Bill not found")
        due = Decimal(str(order.total_amount or 0)) - Decimal(str(order.amount_paid or 0))
        if due <= 0:
            return {"active": False, "expired": True, "paid": True, "remaining_seconds": 0}
        data = await payment_offers.open_offer(scoped, row)
        await scoped.commit()
        return data


@router.post("/api/orders/{number}/payment-offer", include_in_schema=False, dependencies=[Depends(require_admin_key)])
async def create_admin_payment_offer(
    number: str,
    kind: str = Query(pattern="^(advance|reminder)$"),
    db: AsyncSession = Depends(get_db),
):
    """Dashboard/admin-key version of payment-offer creation."""
    order = (
        await db.execute(select(Order).where(Order.order_number == number))
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Bill not found")
    cust = await db.get(Customer, order.customer_id)
    if cust is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    due = Decimal(str(order.total_amount or 0)) - Decimal(str(order.amount_paid or 0))
    if due <= 0:
        raise HTTPException(status_code=400, detail="Is bill ka paisa chukta hai")
    offer = await payment_offers.create_offer(db, order=order, kind=kind, created_by="admin")
    if offer is None:
        raise HTTPException(status_code=400, detail="Bill must be above ₹30 to use this offer")
    link = await bill_link.url_for(db, order)
    link = payment_offers.url_for_offer(link, offer)
    await db.commit()
    return {
        "phone": cust.phone,
        "name": cust.name or "Customer",
        "due": float(due),
        "discount": float(offer.discount_amount),
        "offer_amount": float(offer.offer_amount),
        "link": link,
        "kind": kind,
    }


@router.post("/staff/api/orders/{number}/payment-offer", include_in_schema=False)
async def create_payment_offer(
    number: str,
    kind: str = Query(pattern="^(advance|reminder)$"),
    p: StaffPrincipal = Depends(require_biller()),
    db: AsyncSession = Depends(get_db),
):
    from app.routers.staff_panel import _my_order
    order = await _my_order(db, p, number)
    cust = await db.get(Customer, order.customer_id)
    if cust is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    due = Decimal(str(order.total_amount or 0)) - Decimal(str(order.amount_paid or 0))
    if due <= 0:
        raise HTTPException(status_code=400, detail="Is bill ka paisa chukta hai")
    offer = await payment_offers.create_offer(db, order=order, kind=kind, created_by=p.staff.name)
    if offer is None:
        raise HTTPException(status_code=400, detail="Bill must be above ₹30 to use this offer")
    link = await bill_link.url_for(db, order)
    link = f"{link}?offer={offer.id}" if link else ""
    await db.commit()
    return {
        "phone": cust.phone,
        "name": cust.name or "Customer",
        "due": float(due),
        "discount": float(offer.discount_amount),
        "offer_amount": float(offer.offer_amount),
        "link": link,
        "kind": kind,
    }
