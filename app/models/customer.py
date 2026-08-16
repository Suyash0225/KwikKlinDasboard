"""Customer model — one row per WhatsApp number that has ever messaged us."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class Customer(Base, TenantScoped):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # E.164, e.g. +919876543210. Normalised by app/utils/phone.py before insert.
    phone: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # When THEY last messaged US — this is what the 24h window check reads.
    # Indexed: the customers list sorts on it.
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    # When we last sent a proactive follow-up — throttles Phase 5 follow-ups.
    last_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Customer sent STOP — never message them proactively again (utility too).
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Marketing-only opt-out — utility messages (order updates) still allowed.
    marketing_opt_out: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # When we last sent them a MARKETING message — enforces the frequency cap.
    last_marketing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Agent paused on this thread (owner pressed 'Take over' in Inbox, ya
    # complaint/bura-rating par bot ne khud). Pause HAMESHA ke liye nahi:
    # agent_pause_hours guzarne par agla inbound message use resume kar
    # deta hai — warna customer ka agla normal sawal bhi mar jaata tha.
    agent_paused: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    agent_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Optional — for birthday greetings if the owner fills it in.
    birthday: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Customer {self.phone} name={self.name!r}>"
