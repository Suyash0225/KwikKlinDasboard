"""SQLAlchemy models.

Import everything here so that `import app.models` registers every table on
Base.metadata — Alembic autogenerate depends on this.
"""

from app.models.agent import (
    AuditLog,
    Correction,
    DocChunk,
    FaqEntry,
    OpenQuestion,
    SentEvent,
    SettingKV,
)
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
from app.models.expense import Expense
from app.models.marketing import Campaign, CampaignRecipient, Coupon, CouponRedemption
from app.models.order import Order, OrderStatusHistory, derive_payment_status
from app.models.payment import Payment
from app.models.rate import Rate
from app.models.staff import Staff

__all__ = [
    "AuditLog",
    "Base",
    "Campaign",
    "CampaignRecipient",
    "Conversation",
    "Correction",
    "Coupon",
    "CouponRedemption",
    "Customer",
    "Direction",
    "DocChunk",
    "Escalation",
    "EscalationStatus",
    "Expense",
    "FaqEntry",
    "OpenQuestion",
    "Order",
    "OrderStatus",
    "OrderStatusHistory",
    "Payment",
    "PaymentMethod",
    "PaymentStatus",
    "Rate",
    "SentEvent",
    "SettingKV",
    "Staff",
    "StaffRole",
    "derive_payment_status",
]
