"""SQLAlchemy models.

Import everything here so that `import app.models` registers every table on
Base.metadata — Alembic autogenerate depends on this.
"""

from app.models.agent import (
    AuditLog, Correction, DocChunk, FaqEntry, OpenQuestion, SentEvent, SettingKV,
)
from app.models.base import Base
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.enums import Direction, EscalationStatus, OrderStatus, PaymentMethod, PaymentStatus, StaffRole
from app.models.escalation import Escalation
from app.models.event import OutboundMessage, OutboundThrottle, WebhookEvent
from app.models.expense import Expense
from app.models.marketing import Campaign, Lead, CampaignRecipient, Coupon, CouponRedemption
from app.models.rewards import CustomerReward, RewardRule
from app.models.order import Order, OrderStatusHistory, derive_payment_status
from app.models.payment import Payment
from app.models.polisher import PolisherVendor, PolishBatch, PolisherPayment
from app.models.rate import Rate
from app.models.staff import Staff
from app.models.staff_session import StaffSession
from app.models.task import TASK_CANCELLED, TASK_DONE, TASK_OPEN, Task, TaskMessage
from app.models.tenant import (
    ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_OWNER, ROLE_STAFF, ROLES, TENANT_ACTIVE,
    TENANT_CANCELLED, TENANT_PAST_DUE, TENANT_LOCKED, TENANT_SUSPENDED, TENANT_TRIAL,
    WRITABLE_STATUSES, AdminKey, BillingEvent, CreditLedger, RechargeRequest, Invite,
    Invoice, KpiSnapshot, LoginSession, Tenant, User,
)
from app.models.usage import LlmBudgetReservation, LlmUsage

__all__ = [name for name in globals() if not name.startswith("_")]
