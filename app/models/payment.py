"""Append-only payment ledger — every rupee received, one row each.

orders.amount_paid stays as a derived cache (fast reads, existing code),
but THIS table is the source of truth: it is never updated or deleted,
only inserted into. Reports read from here.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import PaymentMethod


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, name="payment_method")
    )
    # 'manager', staff name, 'agent', 'dashboard' — who recorded it
    recorded_by: Mapped[str] = mapped_column(String(80), default="dashboard")
    note: Mapped[str | None] = mapped_column(String(200))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Payment ₹{self.amount} order={self.order_id}>"
