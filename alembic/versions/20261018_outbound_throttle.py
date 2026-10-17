"""Tenant-scoped shared outbound throttle state.

Revision ID: 20261018_outbound_throttle
Revises: 20261017_order_payment_links
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261018_outbound_throttle"
down_revision = "20261017_order_payment_links"
branch_labels = None
depends_on = None

_POLICY = """(
    COALESCE(current_setting('app.system_context', true), '') = 'on'
    OR (
        NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL
        AND tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    )
)"""


def upgrade() -> None:
    op.create_table(
        "outbound_throttles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("daily_date", sa.Date(), nullable=False),
        sa.Column("daily_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_allowed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_outbound_throttles_tenant_id_tenants"),
        sa.PrimaryKeyConstraint("id", name="pk_outbound_throttles"),
        sa.UniqueConstraint("tenant_id", name="uq_outbound_throttles_tenant"),
    )
    op.create_index("ix_outbound_throttles_tenant_id", "outbound_throttles", ["tenant_id"])
    op.execute("ALTER TABLE outbound_throttles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outbound_throttles FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON outbound_throttles USING {_POLICY} WITH CHECK {_POLICY}")


def downgrade() -> None:
    op.drop_table("outbound_throttles")
