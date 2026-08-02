"""Pydantic request/response models for the internal admin API."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models import OrderStatus, PaymentMethod


class OrderItemIn(BaseModel):
    # extra="allow": billing fields (rate, amount, unit, weight_kg...) ride
    # along into the items JSONB without schema churn.
    model_config = {"extra": "allow"}

    type: str = Field(min_length=1, max_length=60)
    qty: int = Field(default=1, ge=1, le=500)
    service: str | None = Field(default=None, max_length=60)


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


class StatusUpdateIn(BaseModel):
    # Status by NAME, e.g. "IN_WASH" — validated against the enum in the router.
    status: str
    changed_by: str = "manager"


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


class StatusHistoryOut(BaseModel):
    old_status: str | None
    new_status: str
    changed_by: str
    changed_at: datetime
