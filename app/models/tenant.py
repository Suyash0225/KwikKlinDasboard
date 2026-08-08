"""The commercial layer: who bought the software, and who may log in.

A `Tenant` is one laundry business — the thing that has a plan, a
subscription and an owner. Today each tenant still gets its own deployment
(see ARCHITECTURE.md), so an instance normally holds exactly one row; the
table exists so that identity, plan and billing live in DATA, not in .env,
and so a later move to shared multi-tenancy is a config change instead of a
data migration (PRD §4.3).

`User` replaces the single shared X-API-Key: real people, real passwords,
real roles. `LoginSession` is a server-side session token — killable, so a
lost laptop is one DELETE away from safe.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# --- tenant lifecycle -------------------------------------------------------
# trial      : paid nothing yet, full access until trial_ends_at
# active     : subscription paying
# past_due   : payment failed, still usable, dunning running
# read_only  : dunning exhausted — they can SEE everything, write nothing.
#              We never delete a shop's data because a card bounced.
# suspended  : we turned them off (abuse / manual)
# cancelled  : they left
TENANT_TRIAL = "trial"
TENANT_ACTIVE = "active"
TENANT_PAST_DUE = "past_due"
TENANT_READ_ONLY = "read_only"
TENANT_SUSPENDED = "suspended"
TENANT_CANCELLED = "cancelled"

WRITABLE_STATUSES = (TENANT_TRIAL, TENANT_ACTIVE, TENANT_PAST_DUE)

# --- user roles -------------------------------------------------------------
ROLE_OWNER = "OWNER"          # sab kuch, settings + billing samet
ROLE_MANAGER = "MANAGER"      # roz ka kaam; billing/settings nahi
ROLE_STAFF = "STAFF"          # sirf apna kaam
ROLE_ACCOUNTANT = "ACCOUNTANT"  # paisa dekh sakta hai, badal nahi sakta
ROLES = (ROLE_OWNER, ROLE_MANAGER, ROLE_STAFF, ROLE_ACCOUNTANT)


class Tenant(Base):
    """One laundry business that bought (or is trying) the software."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # url-safe handle: kwikklin.app/<slug>, and the instance's own name
    slug: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    shop_name: Mapped[str] = mapped_column(String(120))
    owner_name: Mapped[str] = mapped_column(String(120))
    owner_phone: Mapped[str] = mapped_column(String(20), index=True)
    owner_email: Mapped[str | None] = mapped_column(String(160))
    city: Mapped[str | None] = mapped_column(String(80))

    plan: Mapped[str] = mapped_column(String(24), default="starter")
    status: Mapped[str] = mapped_column(
        String(16), default=TENANT_TRIAL, server_default=TENANT_TRIAL, index=True
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # paid up to — dunning starts after this
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # one-time onboarding fee (PRD §2: is segment mein paisa setup par milta hai)
    setup_fee_paid: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # Razorpay handles
    rzp_customer_id: Mapped[str | None] = mapped_column(String(60))
    rzp_subscription_id: Mapped[str | None] = mapped_column(String(60), index=True)

    # what WE still owe them before they are live (WhatsApp connect etc.)
    onboarding_done: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Tenant {self.slug} {self.plan}/{self.status}>"


class User(Base):
    """A person who logs in. Email is unique WITHIN a tenant, not globally —
    the same person can own two shops."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        Index("ix_users_tenant_role", "tenant_id", "role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(160), index=True)
    phone: Mapped[str | None] = mapped_column(String(20), index=True)
    # scrypt: "scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>" — see services/auth.py.
    # Google se aaye user ka koi password nahi hota: yahan ek aisa nishaan
    # rehta hai jo verify_password() kabhi pass nahi karega.
    password_hash: Mapped[str] = mapped_column(String(255))
    # "password" | "google" — kis rasta se ye banda andar aata hai
    auth_provider: Mapped[str] = mapped_column(
        String(20), default="password", server_default="password"
    )
    # Google ka sthir user id. Email badal sakta hai, ye nahi — isliye
    # account milane ke liye email se zyada bharosemand hai.
    google_sub: Mapped[str | None] = mapped_column(String(60), index=True)
    role: Mapped[str] = mapped_column(String(16), default=ROLE_OWNER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # forces a password change on first login (we mail/WhatsApp a temp one)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<User {self.email} {self.role}>"


class LoginSession(Base):
    """Server-side session. We store only the HASH of the token, so a leaked
    database still cannot be used to log in as anybody."""

    __tablename__ = "login_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BillingEvent(Base):
    """Every Razorpay webhook, stored raw before we act on it.

    Same discipline as webhook_events for WhatsApp: the money trail must
    survive a crash mid-processing, and a replayed event must not charge or
    activate twice (event_id is unique).
    """

    __tablename__ = "billing_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), index=True
    )
    event_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    amount_paise: Mapped[int | None] = mapped_column()
    payload: Mapped[dict | None] = mapped_column(JSONB)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
