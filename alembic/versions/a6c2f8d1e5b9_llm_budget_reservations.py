"""Create tenant-scoped reservations for in-flight AI budget checks.

Revision ID: a6c2f8d1e5b9
Revises: w8f1a6c3d9e2
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a6c2f8d1e5b9"
down_revision = "w8f1a6c3d9e2"
branch_labels = None
depends_on = None

_PREDICATE = (
    "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
    "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    op.create_table(
        "llm_budget_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_usd", sa.Numeric(12, 8), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_llm_budget_reservations_tenant_id_tenants"),
        sa.PrimaryKeyConstraint("id", name="pk_llm_budget_reservations"),
    )
    op.create_index("ix_llm_budget_reservations_tenant_id", "llm_budget_reservations", ["tenant_id"])
    op.create_index("ix_llm_budget_reservations_created_at", "llm_budget_reservations", ["created_at"])
    op.create_index("ix_llm_budget_reservations_expires_at", "llm_budget_reservations", ["expires_at"])

    # New tenant-owned tables must opt in explicitly; the historical RLS
    # migration predates this table.
    op.execute("ALTER TABLE llm_budget_reservations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm_budget_reservations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON llm_budget_reservations "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON llm_budget_reservations")
    op.execute("ALTER TABLE llm_budget_reservations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm_budget_reservations DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_llm_budget_reservations_expires_at", table_name="llm_budget_reservations")
    op.drop_index("ix_llm_budget_reservations_created_at", table_name="llm_budget_reservations")
    op.drop_index("ix_llm_budget_reservations_tenant_id", table_name="llm_budget_reservations")
    op.drop_table("llm_budget_reservations")
