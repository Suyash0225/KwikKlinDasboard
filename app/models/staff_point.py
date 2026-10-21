"""Staff ke points — grahak ki rating se, har order par har aadmi ka ek row.

Delivery ke baad grahak "Bahut achha / Theek tha / Sudhar chahiye" dabata
hai. Us order par jisne dhoya (washer) aur jisne pahunchaya (delivery),
dono ko points milte hain (services/staff_scores.py). Ek order + ek aadmi =
ek hi row (unique), taaki grahak do baar dabaye to points do baar na judein.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class StaffPoint(Base, TenantScoped):
    __tablename__ = "staff_points"
    __table_args__ = (
        UniqueConstraint("order_id", "staff_id", name="uq_staff_points_order_staff"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    staff_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    # good | mid | bad — grahak ne kya dabaya
    rating: Mapped[str] = mapped_column(String(8))
    # washer | delivery — is order par is aadmi ka kaam
    role: Mapped[str] = mapped_column(String(12))
    points: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<StaffPoint {self.role} {self.rating} +{self.points}>"
