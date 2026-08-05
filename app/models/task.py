"""Assigned work the agent tracks end-to-end.

The point of this table: when the owner says "Ravi se bol do Sharma ji ka
order urgent hai", that instruction stops being a message that scrolls away
and becomes a row with an owner, a status and a follow-up clock. The agent
chases the assignee until it hears back, then writes the answer here — so
the dashboard always shows what is actually pending and who is sitting on it.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# status values, kept as plain strings so adding one needs no migration
TASK_OPEN = "OPEN"
TASK_DONE = "DONE"
TASK_CANCELLED = "CANCELLED"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # short human code used in WhatsApp ("done T-14") — unique, searchable
    code: Mapped[str] = mapped_column(String(12), unique=True, index=True)

    # what to do, in the owner's own words (already rewritten to address
    # the assignee directly, never "pucho ki...")
    title: Mapped[str] = mapped_column(Text)

    assigned_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff.id"), index=True
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), index=True
    )

    status: Mapped[str] = mapped_column(
        String(12), default=TASK_OPEN, server_default=TASK_OPEN, index=True
    )
    urgent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    created_by: Mapped[str] = mapped_column(String(40), default="owner")
    # whatever the assignee said back — the agent writes their reply here
    reply: Mapped[str | None] = mapped_column(Text)

    ping_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Task {self.code} {self.status}>"
