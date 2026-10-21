"""Staff ke phone ka Web Push pata — app band ho tab bhi khabar pahunche.

Ek row = ek phone/browser. Browser ka diya `endpoint` (push service ka URL)
aur do chaabiyan (p256dh, auth) — inse sirf wahi phone message padh sakta
hai. Push service 404/410 de to phone ne ijazat wapas le li: row hat jaati
hai (services/web_push.py).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class PushSubscription(Base, TenantScoped):
    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    staff_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff.id", ondelete="CASCADE"), index=True
    )
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    # VAPID "sub" ke liye — jis site se subscribe hua (https://dukaan.example)
    origin: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<PushSubscription staff={self.staff_id}>"
