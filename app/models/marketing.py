"""Marketing tables: campaigns, per-recipient tracking, coupons, redemptions."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class Lead(Base, TenantScoped):
    """Marketing Agent spec: enquiry -> follow-up ladder -> customer."""

    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Per-dukaan unique (index migration mein) — ek hi aadmi do laundry
    # mein poochh-taachh kar sakta hai.
    phone: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(40), default="whatsapp")
    area: Mapped[str | None] = mapped_column(String(120))
    items_text: Mapped[str | None] = mapped_column(String(300))
    stage: Mapped[str] = mapped_column(
        String(12), default="NEW", server_default="NEW", index=True
    )  # NEW|CONTACTED|INTERESTED|CONVERTED|LOST|DORMANT
    followup_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_followup_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Campaign(Base, TenantScoped):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120))
    # RFM segment key ('lapsed', 'high_value', ...) or 'custom'
    segment: Mapped[str] = mapped_column(String(40))
    # message body (window) / template name (outside window)
    message_text: Mapped[str] = mapped_column(Text)
    template_name: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(
        String(12), default="draft", server_default="draft", index=True
    )  # draft | suggested | approved | sending | sent | cancelled
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # why the agent suggested it + expected reach/cost — shown to the owner
    rationale: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict | None] = mapped_column(JSONB)  # counters snapshot
    coupon_code: Mapped[str | None] = mapped_column(String(30))
    created_by: Mapped[str] = mapped_column(String(40), default="owner")  # owner|agent
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint("campaign_id", "customer_id", name="uq_campaign_customer"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(12), default="queued", server_default="queued"
    )  # queued | sent | delivered | read | replied | failed | skipped
    wa_message_id: Mapped[str | None] = mapped_column(String(120), index=True)
    detail: Mapped[str | None] = mapped_column(String(200))  # skip/fail reason
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Coupon(Base, TenantScoped):
    __tablename__ = "coupons"
    __table_args__ = (
        # Code PER DUKAAN unique. Pehle `code` khud primary key tha, yaani
        # poore platform par ek hi "OFF10" — aur har dukaan OFF10 chahti hai.
        UniqueConstraint("tenant_id", "code", name="uq_coupons_tenant_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(30), index=True)  # stored UPPER
    discount_type: Mapped[str] = mapped_column(String(8))  # 'percent' | 'flat'
    value: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    min_order: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    per_customer_limit: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    total_limit: Mapped[int | None] = mapped_column(Integer)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id")
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CouponRedemption(Base):
    __tablename__ = "coupon_redemptions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "coupon_code"], ["coupons.tenant_id", "coupons.code"],
            name="fk_coupon_redemptions_coupon",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # FK ab (tenant_id, coupon_code) -> coupons(tenant_id, code) par hai,
    # __table_args__ mein — kyunki parent ki unique jodi bhi do column ki hai.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), index=True
    )
    coupon_code: Mapped[str] = mapped_column(String(30), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"))
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), index=True
    )
    discount_applied: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
