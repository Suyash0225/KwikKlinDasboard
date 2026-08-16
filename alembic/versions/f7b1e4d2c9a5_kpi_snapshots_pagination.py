"""Phase 1 (CRM scale): kpi_snapshots + pagination indexes.

- kpi_snapshots: roz raat ke dashboard aggregates (JSONB) — trend indicators
  inhi se bante hain, kabhi guess se nahi.
- ix_tenants_created_at: cursor-pagination ka keyset (created_at, id) isi
  par chalta hai; ix_tenants_deleted_at recycle-bin filter ke liye.

Revision ID: f7b1e4d2c9a5
Revises: e2a7c5d9f4b3
Create Date: 2026-08-08

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f7b1e4d2c9a5"
down_revision = "e2a7c5d9f4b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kpi_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("at", sa.Date, nullable=False),
        sa.Column("data", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
    )
    op.create_unique_constraint("uq_kpi_snapshots_at", "kpi_snapshots", ["at"])
    op.create_index("ix_kpi_snapshots_at", "kpi_snapshots", ["at"])
    op.create_index("ix_tenants_created_at", "tenants", ["created_at"])
    op.create_index("ix_tenants_deleted_at", "tenants", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_tenants_deleted_at", table_name="tenants")
    op.drop_index("ix_tenants_created_at", table_name="tenants")
    op.drop_table("kpi_snapshots")
