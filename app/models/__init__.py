"""SQLAlchemy models.

Import everything here so that `import app.models` registers every table on
Base.metadata â€” Alembic autogenerate depends on this.
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
from app.models.event import OutboundMessage, WebhookEvent
from app.models.expense import Expense
from app.models.marketing import Campaign, Lead, CampaignRecipient, Coupon, CouponRedemption
from app.models.order import Order, OrderStatusHistory, derive_payment_status
from app.models.payment import Payment
from app.models.rate import Rate
from app.models.staff import Staff
from app.models.task import TASK_CANCELLED, TASK_DONE, TASK_OPEN, Task
from app.models.tenant import (
    ROLE_ACCOUNTANT,
    ROLE_MANAGER,
    ROLE_OWNER,
    ROLE_STAFF,
    ROLES,
    TENANT_ACTIVE,
    TENANT_CANCELLED,
    TENANT_PAST_DUE,
    TENANT_READ_ONLY,
    TENANT_SUSPENDED,
    TENANT_TRIAL,
    WRITABLE_STATUSES,
    BillingEvent,
    LoginSession,
    Tenant,
    User,
)
from app.models.usage import LlmUsage

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
    "Lead",
    "LlmUsage",
    "OpenQuestion",
    "Order",
    "OrderStatus",
    "OrderStatusHistory",
    "OutboundMessage",
    "Payment",
    "PaymentMethod",
    "PaymentStatus",
    "Rate",
    "SentEvent",
    "SettingKV",
    "Staff",
    "StaffRole",
    "TASK_CANCELLED",
    "TASK_DONE",
    "TASK_OPEN",
    "Task",
    "Tenant",
    "User",
    "LoginSession",
    "BillingEvent",
    "ROLES",
    "ROLE_OWNER",
    "ROLE_MANAGER",
    "ROLE_STAFF",
    "ROLE_ACCOUNTANT",
    "TENANT_TRIAL",
    "TENANT_ACTIVE",
    "TENANT_PAST_DUE",
    "TENANT_READ_ONLY",
    "TENANT_SUSPENDED",
    "TENANT_CANCELLED",
    "WRITABLE_STATUSES",
    "WebhookEvent",
    "derive_payment_status",
]

