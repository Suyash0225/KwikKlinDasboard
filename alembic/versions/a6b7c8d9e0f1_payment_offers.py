"""Timed payment offers for advance/reminder nudges."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a6b7c8d9e0f1"
down_revision = "z3e9a7b4c1d8"
branch_labels = None
depends_on = None

_PREDICATE = (
    "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
    "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    op.create_table(
        "payment_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("original_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("discount_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("offer_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False, server_default="staff"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_payment_offers_order_id", "payment_offers", ["order_id"])
    op.create_index("ix_payment_offers_kind", "payment_offers", ["kind"])
    op.create_index("ix_payment_offers_created_at", "payment_offers", ["created_at"])
    op.execute("ALTER TABLE payment_offers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE payment_offers FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON payment_offers "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON payment_offers")
    op.execute("ALTER TABLE payment_offers NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE payment_offers DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_payment_offers_created_at", table_name="payment_offers")
    op.drop_index("ix_payment_offers_kind", table_name="payment_offers")
    op.drop_index("ix_payment_offers_order_id", table_name="payment_offers")
    op.drop_table("payment_offers")
