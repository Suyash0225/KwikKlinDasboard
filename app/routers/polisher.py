"""Admin API for the external saree polisher ledger."""
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import PolishBatch, PolisherPayment, PolisherVendor
from app.routers.orders import require_admin_key
from app.services import waha

router = APIRouter(prefix="/admin/api/polisher", tags=["external-polisher"], dependencies=[Depends(require_admin_key)])


class VendorIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    phone: str = Field(min_length=8, max_length=32)
    rate_per_saree: Decimal = Field(default=Decimal("25.00"), gt=0, le=10000)


class BatchIn(BaseModel):
    vendor_id: uuid.UUID
    quantity_sent: int = Field(gt=0, le=10000)
    sent_on: date
    notes: str | None = Field(default=None, max_length=2000)


class ReturnIn(BaseModel):
    quantity_returned: int = Field(ge=0, le=10000)
    notes: str | None = Field(default=None, max_length=2000)


class PaymentIn(BaseModel):
    vendor_id: uuid.UUID
    amount: Decimal = Field(gt=0, le=10000000)
    paid_on: date
    method: str = Field(default="cash", min_length=2, max_length=32)
    reference: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


def _vendor(v: PolisherVendor) -> dict:
    return {"id": str(v.id), "name": v.name, "phone": v.phone, "rate_per_saree": float(v.rate_per_saree), "is_active": v.is_active}


async def _notify(phone: str, message: str) -> str:
    if not settings.WAHA_BASE_URL:
        return "not_configured"
    try:
        await waha.send_text(phone, message, link_preview=False)
        return "sent"
    except Exception:
        return "failed"


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)) -> dict:
    vendors = (await db.execute(select(PolisherVendor).where(PolisherVendor.is_active.is_(True)).order_by(PolisherVendor.name))).scalars().all()
    batches = (await db.execute(select(PolishBatch).order_by(PolishBatch.sent_on.desc()))).scalars().all()
    payments = (await db.execute(select(PolisherPayment))).scalars().all()
    # A payable is accrued only for pieces returned as polished, not merely dispatched.
    accrued = sum((Decimal(b.quantity_returned) * b.rate_per_saree for b in batches), Decimal("0"))
    paid = sum((p.amount for p in payments), Decimal("0"))
    returned = sum(b.quantity_returned for b in batches)
    sent = sum(b.quantity_sent for b in batches)
    return {
        "vendors": [_vendor(v) for v in vendors],
        "batches": [{"id": str(b.id), "batch_code": b.batch_code, "vendor_id": str(b.vendor_id), "sent_on": b.sent_on.isoformat(), "quantity_sent": b.quantity_sent, "quantity_returned": b.quantity_returned, "quantity_pending": b.quantity_sent-b.quantity_returned, "rate_per_saree": float(b.rate_per_saree), "amount": float(b.rate_per_saree*b.quantity_sent), "notification_status": b.last_notification_status, "notes": b.notes} for b in batches],
        "payments": [{"id": str(p.id), "vendor_id": str(p.vendor_id), "amount": float(p.amount), "paid_on": p.paid_on.isoformat(), "method": p.method, "notification_status": p.notification_status, "reference": p.reference} for p in payments],
        "totals": {"sarees_sent": sent, "sarees_returned": returned, "sarees_pending": sent-returned, "amount_accrued": float(accrued), "amount_paid": float(paid), "outstanding": float(accrued-paid)},
    }


@router.post("/vendors")
async def create_vendor(payload: VendorIn, db: AsyncSession = Depends(get_db)) -> dict:
    phone = "".join(c for c in payload.phone if c.isdigit())
    if len(phone) == 10:
        phone = "91" + phone
    if len(phone) < 11 or len(phone) > 15:
        raise HTTPException(422, "Enter a valid WhatsApp number, with country code if not an Indian 10-digit number")
    vendor = PolisherVendor(name=payload.name.strip(), phone=phone, rate_per_saree=payload.rate_per_saree)
    db.add(vendor)
    await db.commit()
    await db.refresh(vendor)
    return _vendor(vendor)


@router.post("/batches")
async def create_batch(payload: BatchIn, db: AsyncSession = Depends(get_db)) -> dict:
    vendor = await db.get(PolisherVendor, payload.vendor_id)
    if not vendor or not vendor.is_active:
        raise HTTPException(404, "Active polisher vendor not found")
    count = await db.scalar(select(func.count()).select_from(PolishBatch))
    batch = PolishBatch(
        vendor_id=vendor.id, batch_code=f"POL-{date.today():%y%m%d}-{int(count or 0)+1:04d}",
        sent_on=payload.sent_on, quantity_sent=payload.quantity_sent, quantity_returned=0,
        rate_per_saree=vendor.rate_per_saree, notes=payload.notes,
    )
    db.add(batch)
    await db.commit()
    await db.refresh(batch)
    amount = batch.rate_per_saree * batch.quantity_sent
    status = await _notify(vendor.phone, f"Kwik Klin polish batch {batch.batch_code}\nSaree sent: {batch.quantity_sent}\nRate: ₹{batch.rate_per_saree}/saree\nExpected amount: ₹{amount}\nDate: {batch.sent_on}\nPlease confirm receipt with Kwik Klin.")
    batch.last_notification_status = status
    await db.commit()
    return {"id": str(batch.id), "batch_code": batch.batch_code, "quantity_sent": batch.quantity_sent, "rate_per_saree": float(batch.rate_per_saree), "amount": float(amount), "notification_status": status}


@router.patch("/batches/{batch_id}/return")
async def update_return(batch_id: uuid.UUID, payload: ReturnIn, db: AsyncSession = Depends(get_db)) -> dict:
    batch = await db.get(PolishBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Polish batch not found")
    if payload.quantity_returned > batch.quantity_sent:
        raise HTTPException(422, "Returned quantity cannot exceed quantity sent")
    batch.quantity_returned = payload.quantity_returned
    if payload.notes is not None:
        batch.notes = payload.notes
    await db.commit()
    return {"id": str(batch.id), "quantity_sent": batch.quantity_sent, "quantity_returned": batch.quantity_returned, "quantity_pending": batch.quantity_sent-batch.quantity_returned}


@router.post("/payments")
async def record_payment(payload: PaymentIn, db: AsyncSession = Depends(get_db)) -> dict:
    vendor = await db.get(PolisherVendor, payload.vendor_id)
    if not vendor:
        raise HTTPException(404, "Polisher vendor not found")
    payment = PolisherPayment(vendor_id=vendor.id, amount=payload.amount, paid_on=payload.paid_on, method=payload.method, reference=payload.reference, notes=payload.notes)
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    status = await _notify(vendor.phone, f"Kwik Klin payment recorded\nAmount: ₹{payment.amount}\nDate: {payment.paid_on}\nMethod: {payment.method}\nReference: {payment.reference or '—'}\nThank you.")
    payment.notification_status = status
    await db.commit()
    return {"id": str(payment.id), "amount": float(payment.amount), "notification_status": status}
