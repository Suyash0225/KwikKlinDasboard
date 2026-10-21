"""push_subscriptions — staff ke phone par Web Push (app band ho tab bhi).

Tenant table, wahi RLS policy (d4c8e2f7a915).

Revision ID: 20261021_push_subs
Revises: 20261021_cash_handovers
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261021_push_subs"
down_revision = "20261021_cash_handovers"
branch_labels = None
depends_on = None

_PREDICATE = (
    "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
    "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("staff_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("staff.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.String(200), nullable=False),
        sa.Column("auth", sa.String(100), nullable=False),
        sa.Column("origin", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("endpoint", name="uq_push_subscriptions_endpoint"),
    )
    op.create_index("ix_push_subscriptions_tenant_id", "push_subscriptions", ["tenant_id"])
    op.create_index("ix_push_subscriptions_staff_id", "push_subscriptions", ["staff_id"])
    op.execute("ALTER TABLE push_subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE push_subscriptions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON push_subscriptions USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )
    has_role = op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'kk_app'")).scalar()
    if has_role:
        op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON push_subscriptions TO kk_app")


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON push_subscriptions")
    op.drop_table("push_subscriptions")
