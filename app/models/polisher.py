"""External saree polisher ledger; kept separate from employee staff/payroll."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantScoped


class PolisherVendor(Base, TenantScoped):
    __tablename__ = "polisher_vendors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(32))
    rate_per_saree: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("25.00"))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PolishBatch(Base, TenantScoped):
    __tablename__ = "polish_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("polisher_vendors.id", ondelete="RESTRICT"), index=True)
    batch_code: Mapped[str] = mapped_column(String(32), index=True)
    sent_on: Mapped[date] = mapped_column(Date, index=True)
    quantity_sent: Mapped[int] = mapped_column(Integer)
    quantity_returned: Mapped[int] = mapped_column(Integer, default=0)
    rate_per_saree: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    last_notification_status: Mapped[str] = mapped_column(String(24), default="not_sent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PolisherPayment(Base, TenantScoped):
    __tablename__ = "polisher_payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("polisher_vendors.id", ondelete="RESTRICT"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    paid_on: Mapped[date] = mapped_column(Date, index=True)
    method: Mapped[str] = mapped_column(String(32), default="cash")
    reference: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    notification_status: Mapped[str] = mapped_column(String(24), default="not_sent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
