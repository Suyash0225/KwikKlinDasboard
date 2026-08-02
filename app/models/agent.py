"""Agent infrastructure tables: audit trail, open questions, idempotency,
editable knowledge (FAQ + corrections) and hot-reloadable settings."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditLog(Base):
    """Every agent/scheduler action — who did what, with what, what happened."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    actor_role: Mapped[str] = mapped_column(String(20))   # admin|staff|customer|system
    actor: Mapped[str | None] = mapped_column(String(80))  # phone or name or job id
    action: Mapped[str] = mapped_column(String(60), index=True)  # tool/event name
    args: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[str | None] = mapped_column(Text)      # short outcome text
    ok: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class OpenQuestion(Base):
    """A customer question the bot could not answer — stays open until the
    admin replies, then the answer is relayed back and the row closes."""

    __tablename__ = "open_questions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), index=True
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id")
    )
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(12), default="open", server_default="open", index=True
    )  # open | answered | expired
    answer: Mapped[str | None] = mapped_column(Text)
    asked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SentEvent(Base):
    """Idempotency keys for scheduled sends — the same event never fires twice."""

    __tablename__ = "sent_events"

    # e.g. 'standup:2026-08-03:+918707093136' or 'payrem3:KK-20260801-01'
    event_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FaqEntry(Base):
    """Owner-editable knowledge — the agent answers from these, hot-reloaded."""

    __tablename__ = "faq_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(
        String(12), default="customer", server_default="customer"
    )  # customer | staff | all
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Correction(Base):
    """'When asked X, the right reply is Y' — taught from real conversations."""

    __tablename__ = "corrections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    question: Mapped[str] = mapped_column(Text)
    correct_reply: Mapped[str] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(
        String(12), default="customer", server_default="customer"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SettingKV(Base):
    """Hot-reloadable app settings the owner edits from the UI — no restarts."""

    __tablename__ = "settings_kv"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)  # always {'v': <actual value>}
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
