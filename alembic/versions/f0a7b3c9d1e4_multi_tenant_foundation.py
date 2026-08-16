"""Multi-tenant foundation: tenant_id on every business-data table.

Kya karta hai (3 kaam):
1. 19 business tables mein nullable `tenant_id` UUID column + FK -> tenants.id
   + index. (tenants table pehle se hai — account.py/SaaS wali — nayi nahi
   banayi.)
2. Home tenant resolve karta hai (settings_kv 'home_tenant_slug' -> warna
   sabse purana tenant -> warna 'kwik-klin' tenant CREATE karta hai) aur
   SAARA existing data usi ke under backfill kar deta hai.
3. Fresh DB par home_tenant_slug setting bhi pin kar deta hai.

Columns NULLABLE rehte hain (NOT NULL nahi): API layer abhi tenant_id likhta
nahi hai, is liye NOT NULL har insert tod deta. API-scoping phase mein NOT
NULL + per-tenant unique constraints (phone, rate card, settings key, coupon
code) aayenge.

Reversible: downgrade saare columns/FK/index gira deta hai. (Fallback mein
bana kwik-klin tenant row jaan-bujh kar nahi hataya jaata — tenants table is
migration se pehle ki hai, aur us row par login sessions bane ho sakte hain.)

Revision ID: f0a7b3c9d1e4
Revises: a1f4c72b93de
Create Date: 2026-08-08

"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f0a7b3c9d1e4"
down_revision = "a1f4c72b93de"
branch_labels = None
depends_on = None

# Har business-data table jisme tenant_id lag raha hai. Pure child tables
# (order_status_history, campaign_recipients, coupon_redemptions) yahan NAHI
# hain — unka tenant parent (order/campaign/coupon) se derive hota hai.
# Infra queues (webhook_events, outbound_queue, sent_events) bhi nahi —
# inbound event ka tenant phone_number_id se API phase mein resolve hoga.
TENANT_TABLES = [
    "customers",
    "staff",
    "orders",
    "payments",
    "expenses",
    "rate_card",
    "conversations",
    "escalations",
    "tasks",
    "leads",
    "campaigns",
    "coupons",
    "faq_entries",
    "corrections",
    "doc_chunks",
    "open_questions",
    "settings_kv",
    "audit_log",
    "llm_usage",
]

HOME_SLUG_FALLBACK = "kwik-klin"


def _resolve_home_tenant_id(conn) -> str:
    """Home tenant dhundo; na mile to banao. Returns id as str."""
    # 1) Pinned slug from settings (bootstrap_home_tenant.py sets this).
    row = conn.execute(
        sa.text("SELECT value->>'v' FROM settings_kv WHERE key = 'home_tenant_slug'")
    ).first()
    slug = row[0] if row and row[0] else None
    if slug:
        row = conn.execute(
            sa.text("SELECT id FROM tenants WHERE slug = :slug"), {"slug": slug}
        ).first()
        if row:
            return str(row[0])

    # 2) Oldest tenant (same fallback auth.home_tenant() uses).
    row = conn.execute(
        sa.text("SELECT id, slug FROM tenants ORDER BY created_at ASC LIMIT 1")
    ).first()
    if row:
        return str(row[0])

    # 3) Fresh DB: create the default tenant. Placeholder owner details —
    #    the owner edits these from the control panel later.
    new_id = str(uuid.uuid4())
    conn.execute(
        sa.text(
            "INSERT INTO tenants (id, slug, shop_name, owner_name, owner_phone,"
            " plan, status, setup_fee_paid, onboarding_done)"
            " VALUES (:id, :slug, 'Kwik Klin', 'Owner', '+910000000000',"
            " 'starter', 'active', false, true)"
        ),
        {"id": new_id, "slug": HOME_SLUG_FALLBACK},
    )
    # Pin it so auth.home_tenant() and future runs agree on the answer.
    conn.execute(
        sa.text(
            "INSERT INTO settings_kv (key, value) "
            "VALUES ('home_tenant_slug', :val::jsonb) "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {"val": '{"v": "%s"}' % HOME_SLUG_FALLBACK},
    )
    return new_id


def upgrade() -> None:
    # --- 1. Columns + FKs + indexes (schema) ---
    for t in TENANT_TABLES:
        op.add_column(
            t, sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            f"fk_{t}_tenant_id_tenants", t, "tenants", ["tenant_id"], ["id"]
        )
        op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])

    # --- 2. Backfill: saara existing Kwik Klin data home tenant ke under ---
    conn = op.get_bind()
    home_id = _resolve_home_tenant_id(conn)
    for t in TENANT_TABLES:
        conn.execute(
            sa.text(f"UPDATE {t} SET tenant_id = :tid WHERE tenant_id IS NULL"),
            {"tid": home_id},
        )


def downgrade() -> None:
    for t in reversed(TENANT_TABLES):
        op.drop_index(f"ix_{t}_tenant_id", table_name=t)
        op.drop_constraint(f"fk_{t}_tenant_id_tenants", t, type_="foreignkey")
        op.drop_column(t, "tenant_id")
