"""Durability tables: the inbound webhook journal and the outbound retry queue.

Together they guarantee no message is ever lost to a crash:
- Every webhook body is journaled BEFORE any processing. If processing dies,
  a scheduler job replays the event from the journal.
- Every outbound send that fails on a transient error (network / 5xx / 429)
  is queued here and retried with backoff until it goes out or dead-letters.
"""

import uuid
from datetime import datetime

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WebhookEvent(Base):
    """Raw inbound webhook payload, persisted before we touch it."""

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(String(10))  # meta | dotpe
    # sha256 of the raw body — the same delivery journals exactly once
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(12), default="received", server_default="received", index=True
    )  # received | processed | failed | dead
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboundMessage(Base):
    """A send that failed transiently — retried by the scheduler with backoff."""

    __tablename__ = "outbound_queue"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    to_phone: Mapped[str] = mapped_column(String(20))
    # kwargs for send_message: text/buttons/template_name/template_params/sent_by
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(12), default="queued", server_default="queued", index=True
    )  # queued | sent | dead
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboundThrottle(Base, TenantScoped):
    """Cross-worker reservation state for promotional send rate limits."""

    __tablename__ = "outbound_throttles"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_outbound_throttles_tenant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    daily_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    daily_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_allowed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
