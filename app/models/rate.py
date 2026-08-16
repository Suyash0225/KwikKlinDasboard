"""Rate card — service × garment pricing the shop can edit from Settings.

New Bill pulls rates from here (manual override still allowed at billing
time). Rows are soft-disabled via is_active, never hard-deleted by the UI,
so old bills' context survives.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class Rate(Base, TenantScoped):
    __tablename__ = "rate_card"
    __table_args__ = (
        # one price per service+garment combination
        UniqueConstraint("service", "garment", name="uq_service_garment"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # "Dry Clean", "Wash & Iron", "Wash & Fold (kg)"...
    service: Mapped[str] = mapped_column(String(60))
    # Garment for per-piece rates ("Shirt", "Saree"); empty string for
    # per-kg services (NULLs don't play well with the unique constraint).
    garment: Mapped[str] = mapped_column(String(60), default="")
    unit: Mapped[str] = mapped_column(String(2), default="pc")  # 'pc' | 'kg'
    rate: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Rate {self.service}/{self.garment or '-'} ₹{self.rate}/{self.unit}>"
