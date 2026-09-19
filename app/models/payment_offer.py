"""Temporary customer payment offers for advance/reminder nudges."""

import secrets
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


def _new_offer_code() -> str:
    """Short customer-facing offer code; 48 bits of random entropy."""
    return secrets.token_hex(6).upper()


class PaymentOffer(Base, TenantScoped):
    __tablename__ = "payment_offers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Short code used in customer-facing URLs; UUID remains the internal primary key.
    offer_code: Mapped[str] = mapped_column(String(12), unique=True, index=True, default=_new_offer_code)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(12), index=True)  # advance | reminder
    original_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    offer_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    duration_seconds: Mapped[int] = mapped_column(Integer, default=900, server_default="900")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(80), default="staff", server_default="staff")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    def __repr__(self) -> str:
        return f"<PaymentOffer {self.kind} {self.offer_amount}>"
