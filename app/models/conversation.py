"""Conversation log — every inbound and outbound message, one row each.

Exactly one of customer_id / staff_id must be set (a message belongs to a
customer thread OR a staff thread, never both, never neither). Enforced by a
database check constraint, not just application code.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped
from app.models.enums import Direction


class Conversation(Base, TenantScoped):
    __tablename__ = "conversations"
    __table_args__ = (
        # XOR: one side NULL, the other NOT NULL.
        CheckConstraint(
            "(customer_id IS NULL) <> (staff_id IS NULL)",
            name="exactly_one_participant",
        ),
        # Thread views always read "WHERE participant = ? ORDER BY created_at
        # DESC LIMIT n" — these composites keep that a pure index scan.
        Index("ix_conversations_customer_created", "customer_id", "created_at"),
        Index("ix_conversations_staff_created", "staff_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), index=True
    )
    staff_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff.id"), index=True
    )

    direction: Mapped[Direction] = mapped_column(Enum(Direction, name="message_direction"))
    message_text: Mapped[str] = mapped_column(Text)

    # WhatsApp's message id (wamid...). Unique so webhook retries can't
    # insert the same inbound message twice.
    wa_message_id: Mapped[str | None] = mapped_column(String(120), unique=True)

    # Meta ke paise ka hisaab: 'service' = customer ke 24h window mein
    # diya gaya jawab (FREE), 'utility' = order/payment template,
    # 'marketing' = offer/campaign (sabse mehnga). Quota sirf billable
    # (utility+marketing) ginta hai — free replies kabhi block nahi hote.
    billing_category: Mapped[str | None] = mapped_column(String(10))
    # Who authored an OUTBOUND message: "bot", "manager", "system".
    # NULL on inbound rows (sender is the participant) and on old rows.
    sent_by: Mapped[str | None] = mapped_column(String(40))

    # Delivery state of an OUTBOUND message, straight from Meta's status
    # webhook: sent -> delivered -> read (or failed). This is what the
    # Inbox's ✓ / ✓✓ / blue ✓✓ means — before it existed the UI drew a blue
    # double tick on everything, which claimed messages were read that were
    # never even delivered. NULL on inbound and on rows sent before this.
    status: Mapped[str | None] = mapped_column(String(12))

    # wamid of the message this one quotes (WhatsApp reply). NULL = not a reply.
    reply_to_wamid: Mapped[str | None] = mapped_column(String(120))

    # Filled by Phase 4 (intent classification). Plain string, not an enum,
    # so adding new intents never needs a migration.
    intent: Mapped[str | None] = mapped_column(String(50))
    # {"model": ..., "input_tokens": ..., "output_tokens": ..., "latency_ms": ...}
    ai_response_meta: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        who = f"customer={self.customer_id}" if self.customer_id else f"staff={self.staff_id}"
        return f"<Conversation {self.direction.name} {who}>"
