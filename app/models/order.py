"""Order model + status history.

PAYMENT RULE (the single place this logic lives — never set payment_status
directly anywhere else):

    amount_paid <= 0                          -> UNPAID
    0 < amount_paid < total_amount            -> PARTIAL
    amount_paid >= total_amount (and total set) -> PAID
    total_amount not set but amount_paid > 0  -> PARTIAL
      (we can't claim PAID against an unknown total)

Call order.recalculate_payment_status() after ANY change to amount_paid or
total_amount. Overpayment counts as PAID.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text, func
from sqlalchemy import Date as SADate
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import OrderStatus, PaymentMethod, PaymentStatus


def derive_payment_status(
    total_amount: Decimal | None, amount_paid: Decimal | None
) -> PaymentStatus:
    """The one and only payment-status rule. See module docstring."""
    if amount_paid is None or amount_paid <= 0:
        return PaymentStatus.UNPAID
    if total_amount is None or amount_paid < total_amount:
        return PaymentStatus.PARTIAL
    return PaymentStatus.PAID


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Human-facing, e.g. "LDY-20260801-0042". Generation logic comes in
    # Phase 3 (order_service). Unique constraint protects us regardless.
    order_number: Mapped[str] = mapped_column(String(30), unique=True, index=True)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), index=True
    )

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"), default=OrderStatus.RECEIVED
    )

    # [{"type": "shirt", "qty": 3, "service": "wash_iron"}, ...]
    items: Mapped[list] = mapped_column(JSONB, default=list)

    # --- Payments (see module docstring for the rule) ---
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0"), server_default="0"
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), default=PaymentStatus.UNPAID
    )
    payment_method: Mapped[PaymentMethod | None] = mapped_column(
        Enum(PaymentMethod, name="payment_method")
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Dates ---
    pickup_date: Mapped[date | None] = mapped_column(SADate)
    # THE promise to the customer. Ground rule: never quote a date in a
    # message unless it is already written here.
    expected_delivery: Mapped[date | None] = mapped_column(SADate)
    actual_delivery: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Assignment ---
    assigned_washer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff.id"), index=True
    )
    assigned_delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff.id"), index=True
    )

    # INTERNAL ONLY. Delay reasons ("paani nahi aaya") live here for the
    # manager. This column must NEVER be included in a customer message.
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def recalculate_payment_status(self) -> None:
        """Re-derive payment_status from the money columns.

        Call after any change to amount_paid or total_amount.
        """
        self.payment_status = derive_payment_status(self.total_amount, self.amount_paid)

    def __repr__(self) -> str:
        return f"<Order {self.order_number} {self.status.name}>"


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), index=True
    )
    # Null for the very first entry (order creation has no previous status).
    old_status: Mapped[OrderStatus | None] = mapped_column(
        Enum(OrderStatus, name="order_status")
    )
    new_status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, name="order_status"))
    # Who caused it: "staff:ravi", "customer", or "system".
    changed_by: Mapped[str] = mapped_column(String(80))
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        old = self.old_status.name if self.old_status else "-"
        return f"<StatusChange {old} -> {self.new_status.name} by {self.changed_by}>"
