"""SQLAlchemy models.

Import everything here so that `import app.models` registers every table on
Base.metadata — Alembic autogenerate depends on this.
"""

from app.models.base import Base
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.enums import (
    Direction,
    EscalationStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    StaffRole,
)
from app.models.escalation import Escalation
from app.models.order import Order, OrderStatusHistory, derive_payment_status
from app.models.staff import Staff

__all__ = [
    "Base",
    "Conversation",
    "Customer",
    "Direction",
    "Escalation",
    "EscalationStatus",
    "Order",
    "OrderStatus",
    "OrderStatusHistory",
    "PaymentMethod",
    "PaymentStatus",
    "Staff",
    "StaffRole",
    "derive_payment_status",
]
