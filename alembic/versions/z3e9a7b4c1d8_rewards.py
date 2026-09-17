"""Loyalty rewards: reward_rules + customer_rewards (RLS), coupons.customer_id.

Coupon ab kisi EK grahak ka ho sakta hai (customer_id): reward wala coupon
doosra grahak use nahi kar sakta — validate_coupon yahi check karta hai.

Revision ID: z3e9a7b4c1d8
Revises: z2d8f6a2c9e7
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "z3e9a7b4c1d8"
down_revision = "z2d8f6a2c9e7"
branch_labels = None
depends_on = None

_PREDICATE = (
    "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
    "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {table} USING {_PREDICATE} WITH CHECK {_PREDICATE}")


def upgrade() -> None:
    op.add_column(
        "coupons",
        sa.Column("customer_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_coupons_customer_id", "coupons", ["customer_id"])

    op.create_table(
        "reward_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("threshold", sa.Numeric(10, 2), nullable=False),
        sa.Column("reward_type", sa.String(8), nullable=False),
        sa.Column("value", sa.Numeric(10, 2), nullable=False),
        sa.Column("max_discount", sa.Numeric(10, 2), nullable=True),
        sa.Column("min_order", sa.Numeric(10, 2), nullable=True),
        sa.Column("valid_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_reward_rules_tenant_id", "reward_rules", ["tenant_id"])

    op.create_table(
        "customer_rewards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rule_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("reward_rules.id", ondelete="SET NULL"), nullable=True),
        sa.Column("coupon_code", sa.String(30), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="earned"),
        sa.Column("earned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger_order_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("used_order_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(200), nullable=True),
    )
    op.create_index("ix_customer_rewards_tenant_id", "customer_rewards", ["tenant_id"])
    op.create_index("ix_customer_rewards_customer_id", "customer_rewards", ["customer_id"])
    op.create_index("ix_customer_rewards_coupon_code", "customer_rewards", ["coupon_code"])
    op.create_index("ix_customer_rewards_status", "customer_rewards", ["status"])

    has_role = _role_exists()
    for t in ("reward_rules", "customer_rewards"):
        _rls(t)
        if has_role:   # app ka NOSUPERUSER role (kk_app) — baaki tenant tables jaisa
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO kk_app")


def _role_exists() -> bool:
    conn = op.get_bind()
    return bool(conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'kk_app'")).scalar())


def downgrade() -> None:
    for t in ("customer_rewards", "reward_rules"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.drop_table(t)
    op.drop_index("ix_coupons_customer_id", table_name="coupons")
    op.drop_column("coupons", "customer_id")
