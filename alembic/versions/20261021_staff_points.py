"""staff_points — grahak ki rating se staff ke points (washer + delivery).

Tenant table, wahi RLS policy (d4c8e2f7a915).

Revision ID: 20261021_staff_points
Revises: 20261021_push_subs
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261021_staff_points"
down_revision = "20261021_push_subs"
branch_labels = None
depends_on = None

_PREDICATE = (
    "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
    "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    op.create_table(
        "staff_points",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("staff_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("staff.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating", sa.String(8), nullable=False),
        sa.Column("role", sa.String(12), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("order_id", "staff_id", name="uq_staff_points_order_staff"),
    )
    op.create_index("ix_staff_points_tenant_id", "staff_points", ["tenant_id"])
    op.create_index("ix_staff_points_staff_id", "staff_points", ["staff_id"])
    op.create_index("ix_staff_points_order_id", "staff_points", ["order_id"])
    op.create_index("ix_staff_points_created_at", "staff_points", ["created_at"])
    op.execute("ALTER TABLE staff_points ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE staff_points FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON staff_points USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )
    has_role = op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'kk_app'")).scalar()
    if has_role:
        op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON staff_points TO kk_app")


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON staff_points")
    op.drop_table("staff_points")
