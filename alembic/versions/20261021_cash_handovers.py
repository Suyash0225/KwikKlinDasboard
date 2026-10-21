"""cash_handovers — delivery wale ka shaam ka cash hisaab, manager ki pushti.

Tenant table, isliye wahi RLS policy jo baaki tenant tables par hai
(d4c8e2f7a915 / r2a8c5d3f9e7).

Revision ID: 20261021_cash_handovers
Revises: 20261021_pending_intake
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261021_cash_handovers"
down_revision = "20261021_pending_intake"
branch_labels = None
depends_on = None

_PREDICATE = (
    "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
    "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    op.create_table(
        "cash_handovers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("staff_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("staff.id", ondelete="SET NULL")),
        sa.Column("staff_name", sa.String(120), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cash_collected", sa.Numeric(10, 2), nullable=False),
        sa.Column("upi_collected", sa.Numeric(10, 2), nullable=False),
        sa.Column("payments_count", sa.Integer(), nullable=False),
        sa.Column("handed_cash", sa.Numeric(10, 2), nullable=False),
        sa.Column("note", sa.String(200)),
        sa.Column("status", sa.String(12), nullable=False, server_default="PENDING"),
        sa.Column("received_cash", sa.Numeric(10, 2)),
        sa.Column("confirmed_by", sa.String(120)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_cash_handovers_tenant_id", "cash_handovers", ["tenant_id"])
    op.create_index("ix_cash_handovers_staff_id", "cash_handovers", ["staff_id"])
    op.create_index("ix_cash_handovers_period_end", "cash_handovers", ["period_end"])
    op.create_index("ix_cash_handovers_status", "cash_handovers", ["status"])
    op.execute("ALTER TABLE cash_handovers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cash_handovers FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON cash_handovers USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )
    # app ka NOSUPERUSER role (kk_app) jahan ho — baaki tenant tables jaisa (z3e9a7b4c1d8)
    has_role = op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'kk_app'")).scalar()
    if has_role:
        op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON cash_handovers TO kk_app")


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON cash_handovers")
    op.drop_table("cash_handovers")
