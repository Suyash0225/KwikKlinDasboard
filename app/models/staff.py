"""Staff model — the washer and the delivery person."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped
from app.models.enums import StaffRole


class Staff(Base, TenantScoped):
    __tablename__ = "staff"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Unique (tenant_id, phone) par hai — do alag laundry ek hi aadmi ka
    # number rakh sakti hain (migration n7c4e1b8d5a2).
    phone: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[StaffRole] = mapped_column(Enum(StaffRole, name="staff_role"))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Staff are also WhatsApp users to Meta — same 24h window rule applies.
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- staff panel ka login ---
    # Khali = is aadmi ka panel account bana hi nahi (sirf WhatsApp par hai).
    # Staff khud account nahi bana sakta; owner/manager banata hai.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Staff {self.name} ({self.role.name}) {self.phone}>"
