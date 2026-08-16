"""Staff panel ka server-side session.

Alag table isliye, na ki users wale LoginSession mein ghusa kar: staff ek
DUKAAN KA AADMI hai (staff row), dashboard user nahi. Dono ko ek table
mein milane par har query mein "ye kis tarah ka session hai" poochna
padta, aur ek galti se delivery boy ko owner ka panel mil jaata.

Token kabhi plain nahi rakha jaata — sirf uska sha256. Row maar do to
phone kho jaane par bhi access khatam (server-side revoke).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class StaffSession(Base, TenantScoped):
    __tablename__ = "staff_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    staff_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(200))

    def __repr__(self) -> str:
        return f"<StaffSession staff={self.staff_id}>"
