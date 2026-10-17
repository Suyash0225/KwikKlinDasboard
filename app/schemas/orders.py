"""Pydantic request/response models for the internal admin API."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.models import PaymentMethod


class PieceIn(BaseModel):
    """KG wali line ke andar ke kapde — sirf ginti, daam nahi.

    Bill wazan se banta hai (3.5 kg x ₹60), par counter par likhna zaroori
    hai ki bore mein kya-kya hai: wapas dete waqt "mera ek kurta kam hai"
    ka jawab yahi list hai.
    """

    type: str = Field(min_length=1, max_length=60)
    qty: int = Field(default=1, ge=1, le=999)

    @field_validator("type")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("cloth name is empty")
        return v


class OrderItemIn(BaseModel):
    # extra="allow": billing fields (rate, amount, unit, weight_kg...) ride
    # along into the items JSONB without schema churn.
    model_config = {"extra": "allow"}

    type: str = Field(min_length=1, max_length=60)
    # int | float: dashboard "Qty / kg" 2.5 bhejta hai (step 0.1) aur pehle
    # yahan sirf int tha — 2.5 kg ka bill 422 par gir jaata tha. Union smart
    # mode 2 ko int hi rakhta hai (JSON/receipt mein "2", "2.0" nahi).
    qty: int | float = Field(default=1, gt=0, le=500)

    @field_validator("qty")
    @classmethod
    def _whole_qty_as_int(cls, v):
        return int(v) if isinstance(v, float) and v.is_integer() else v
    service: str | None = Field(default=None, max_length=60)
    # KG line ke kapde (ginti) — extra="allow" ke bharose nahi chhoda, taaki
    # ulta-seedha JSON items mein na baith jaye
    pieces: list[PieceIn] | None = Field(default=None, max_length=50)


class OrderCreateIn(BaseModel):
    customer_phone: str
    customer_name: str | None = None
    items: list[OrderItemIn] = Field(min_length=1)
    total_amount: Decimal | None = Field(default=None, ge=0)
    discount_amount: Decimal | None = Field(default=None, ge=0)
    gst_amount: Decimal | None = Field(default=None, ge=0)
    # Advance paid at the counter — recorded as a payment right after create.
    advance_amount: Decimal | None = Field(default=None, ge=0)
    advance_method: PaymentMethod | None = None
    pickup_date: date | None = None
    expected_delivery: date | None = None
    notes: str | None = None
    # marketing coupon — validated + redeemed server-side
    coupon_code: str | None = None
    # ⚡ Urgent: order sabse pehle, work order par URGENT. Extra charge (agar
    # ho) items mein ek line banke aata hai — kind="urgent_charge".
    priority: str = Field(default="normal", pattern="^(normal|urgent)$")
    # "New bill" kholne se Save tak kitne second (turnaround tracking)
    bill_seconds: int | None = None


class DeliverPickIn(BaseModel):
    line: int = Field(ge=0, le=200)
    piece: int | None = Field(default=None, ge=0, le=200)
    qty: int = Field(ge=0, le=9999)


class DeliverIn(BaseModel):
    # None = saare kapde. Warna har line/kapde ki ginti (12 mein se 8).
    items: list[DeliverPickIn] | None = Field(default=None, max_length=200)


class StatusUpdateIn(BaseModel):
    # Status by NAME, e.g. "IN_WASH" — validated against the enum in the router.
    status: str
    changed_by: str = "manager"


class BillingAdjustmentIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    amount: Decimal = Field(gt=0, le=1000000)
    kind: str = Field(default="extra_charge", pattern="^(extra_charge|due_charge)$")
    note: str | None = Field(default=None, max_length=300)
    changed_by: str = Field(default="dashboard", max_length=80)


class PaymentIn(BaseModel):
    amount: Decimal = Field(gt=0)
    method: PaymentMethod


class DeliveryDateIn(BaseModel):
    expected_delivery: date
    internal_reason: str | None = None
    changed_by: str = "manager"


class OrderOut(BaseModel):
    order_number: str
    status: str
    customer_phone: str
    customer_name: str | None
    items: list
    total_amount: Decimal | None
    discount_amount: Decimal | None = None
    gst_amount: Decimal | None = None
    amount_paid: Decimal
    payment_status: str
    expected_delivery: date | None
    pickup_date: date | None
    created_at: datetime
    notes: str | None = None  # included only in single-order admin view
    billing_adjustments: list = []


class StatusHistoryOut(BaseModel):
    old_status: str | None
    new_status: str
    changed_by: str
    changed_at: datetime
