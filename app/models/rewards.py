"""Loyalty rewards: dukaan ke niyam aur grahak ko mile inaam (services/rewards.py)."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class RewardRule(Base, TenantScoped):
    """Ek niyam: shart (N bills / ₹X spend, kitne din mein) -> inaam (flat / percent)."""

    __tablename__ = "reward_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10))                 # bills | spend
    window_days: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    threshold: Mapped[Decimal] = mapped_column(Numeric(10, 2))    # bills ki ginti ya rakam
    reward_type: Mapped[str] = mapped_column(String(8))           # flat | percent
    value: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    max_discount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    min_order: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    valid_days: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CustomerReward(Base, TenantScoped):
    """Grahak ko mila inaam — coupon ke saath. status: earned | used | cancelled | expired."""

    __tablename__ = "customer_rewards"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reward_rules.id", ondelete="SET NULL")
    )
    coupon_code: Mapped[str] = mapped_column(String(30), index=True)
    label: Mapped[str] = mapped_column(String(60))                # "₹100 off" — niyam badle to bhi yahi
    status: Mapped[str] = mapped_column(String(10), default="earned", server_default="earned", index=True)
    earned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    used_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(200))
