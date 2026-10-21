"""Shaam ka cash hisaab — delivery wala jo paisa laaya, manager ne gin kar liya.

Payment ledger (payments) batata hai ki grahak se kitna aaya. Ye table
batati hai ki wo paisa staff ke haath se dukaan tak pahuncha ya nahi. Ek
row = ek baar haath badla: staff ne bola "itna cash de raha hoon", manager
ne gin kar likha "itna mila". Dono rakam alag rehti hain — farak chhupta
nahi, aur kisi ki baat mitti nahi.

Kaunse payment is hisaab mein: us staff ke naam (payments.recorded_by) par,
period_start ke baad aur period_end tak. Agla hisaab wahin se shuru hota
hai jahan pichhla khatam hua, isliye ek rupaya do baar nahi ginta.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped

HANDOVER_PENDING = "PENDING"
HANDOVER_CONFIRMED = "CONFIRMED"


class CashHandover(Base, TenantScoped):
    __tablename__ = "cash_handovers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    staff_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff.id", ondelete="SET NULL"), index=True
    )
    # naam ki copy — staff hatne ke baad bhi hisaab padha ja sake
    staff_name: Mapped[str] = mapped_column(String(120))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # ledger ke hisaab se is muddat mein kitna aaya
    cash_collected: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    upi_collected: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    payments_count: Mapped[int] = mapped_column(Integer)
    # staff ne kitna cash diya (kharcha kaat kar kam ho sakta hai — note mein wajah)
    handed_cash: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    note: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default=HANDOVER_PENDING, index=True)
    # manager ne gin kar kitna paaya
    received_cash: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    confirmed_by: Mapped[str | None] = mapped_column(String(120))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<CashHandover {self.staff_name} ₹{self.handed_cash} {self.status}>"
