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
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.services.secrets import EncryptedText

# --- tenant lifecycle -------------------------------------------------------
# trial      : paid nothing yet, FULL access until trial_ends_at (7 din)
# active     : subscription paying, full access
# past_due   : trial/subscription khatam, payment nahi — READ-ONLY.
#              Data poora dikhta hai, kuch delete nahi hota. 30 din grace.
# locked     : grace bhi khatam — dashboard band. Data phir bhi SAFE hai,
#              kabhi delete nahi hota; payment aate hi wapas active.
# suspended  : we turned them off (abuse / manual)
# cancelled  : they left
#
# (Legacy note: purana "read_only" state migration b9f1a6c3e8d2 mein
#  past_due mein merge ho gaya — ab past_due HI read-only grace hai.)
TENANT_TRIAL = "trial"
TENANT_ACTIVE = "active"
TENANT_PAST_DUE = "past_due"
TENANT_LOCKED = "locked"
TENANT_SUSPENDED = "suspended"
TENANT_CANCELLED = "cancelled"

# past_due ab writable NAHI hai — wahi to read-only grace hai.
WRITABLE_STATUSES = (TENANT_TRIAL, TENANT_ACTIVE)

# past_due -> locked hone se pehle kitne din ka grace.
GRACE_DAYS = 30

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

    # --- WhatsApp (per-tenant, official Meta Cloud API) -----------------
    # Ek tenant = ek WhatsApp number. Inbound webhook metadata ke
    # phone_number_id se isi column par route hota hai; outbound isi tenant
    # ke token se jaata hai. Khali = .env creds (home/legacy single-shop).
    # Token API responses mein HAMESHA masked — kabhi wapas nahi bheja jaata.
    wa_phone_number_id: Mapped[str | None] = mapped_column(
        String(30), unique=True, index=True
    )
    wa_waba_id: Mapped[str | None] = mapped_column(String(30))
    # DB mein encrypted (app/services/secrets.py) — Python mein plain.
    # Kabhi API response mein poora mat bhejo; sirf mask (••••1234).
    wa_token: Mapped[str | None] = mapped_column(EncryptedText)

    # what WE still owe them before they are live (WhatsApp connect etc.)
    onboarding_done: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    notes: Mapped[str | None] = mapped_column(Text)
    # Chhote labels: ["vip", "referral"] — profile chips + filters.
    tags: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    # Recharge balance: plan/override limit KHATAM hone ke baad har extra
    # message/AI call ek credit khaata hai (services/quota.py). 0 = koi
    # top-up nahi, yani limit hi aakhri deewar.
    ai_credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    wa_credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Vendor ke per-client limit overrides (plan defaults ke upar):
    # {"ai_usage_limit": 500, "whatsapp_message_limit": 1000,
    #  "max_orders_month": 100, "max_staff": 5}. Khali = plan ke limits.
    limit_overrides: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    # Soft delete: set = recycle bin mein hai (list se gayab, data salamat).
    # Hard purge alag, explicit, danger-scoped action hai — kabhi automatic nahi.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # monthly | annual — checkout ka default; Razorpay subscription isi se banti hai
    billing_cycle: Mapped[str] = mapped_column(
        String(10), default="monthly", server_default="monthly"
    )
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


class Invoice(Base):
    """Har successful charge ki ek receipt — per tenant, kabhi delete nahi.

    billing_events raw webhook log hai; ye USKI saaf-suthri, dikhaane layak
    shakal hai: kitna, kis plan ka, kis period ka. rzp_payment_id unique —
    payment.captured + subscription.charged dono aayen to bhi ek hi row.
    """

    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), index=True
    )
    rzp_payment_id: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    rzp_subscription_id: Mapped[str | None] = mapped_column(String(60), index=True)
    plan: Mapped[str] = mapped_column(String(24))
    cycle: Mapped[str] = mapped_column(String(10), default="monthly")  # monthly|annual
    amount_paise: Mapped[int] = mapped_column()
    currency: Mapped[str] = mapped_column(String(8), default="INR", server_default="INR")
    status: Mapped[str] = mapped_column(String(12), default="paid", server_default="paid")
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Invite(Base):
    """Set-password invite link — plaintext password KABHI create/store/send
    nahi hota. Token ka sirf sha256 hash yahan; raw token sirf link mein.
    """

    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), index=True
    )
    email: Mapped[str] = mapped_column(String(160), index=True)
    role: Mapped[str] = mapped_column(String(16), default=ROLE_OWNER)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invited_by: Mapped[str | None] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AdminKey(Base):
    """Vendor-panel ki per-admin keys — shared env key ki jagah.

    Raw key sirf create ke response mein EK baar; DB mein sirf hash.
    level: read < write < danger (danger = delete/reset/keys manage).
    Rotation = nayi banao, purani revoke karo.
    """

    __tablename__ = "admin_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    label: Mapped[str] = mapped_column(String(60))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    level: Mapped[str] = mapped_column(String(10), default="write")  # read|write|danger
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KpiSnapshot(Base):
    """Roz raat ke dashboard KPIs — trends inhi se bante hain (guess se nahi)."""

    __tablename__ = "kpi_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    at: Mapped[date] = mapped_column(Date, unique=True, index=True)
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CreditLedger(Base):
    """Har recharge/deduction ka record — kab, kitna, kisne, kyun.

    tenants.ai_credits/wa_credits sirf tez balance hai; sach ye table hai.
    """

    __tablename__ = "credit_ledger"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(4))          # ai | wa
    amount: Mapped[int] = mapped_column(Integer)          # + top-up, - removal
    balance_after: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(80))
    # Validity: NULL = kabhi khatam nahi (default). Set ho to nightly sweep
    # us din ke baad bacha hua hissa balance se kaat deta hai.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    # sweep ne is lot ko process kar liya — dobara kabhi nahi kaatega
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class RechargeRequest(Base):
    """Client ne apne billing page se recharge maanga — vendor approve
    karta hai (paisa GPay/UPI se alag aata hai) aur credits chadh jaate hain."""

    __tablename__ = "recharge_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    pack: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(4))          # ai | wa
    units: Mapped[int] = mapped_column(Integer)
    amount_inr: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(12), default="pending", server_default="pending", index=True
    )  # pending | approved | rejected
    note: Mapped[str | None] = mapped_column(String(200))   # client ka UPI ref
    requested_by: Mapped[str | None] = mapped_column(String(160))
    decided_by: Mapped[str | None] = mapped_column(String(80))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
