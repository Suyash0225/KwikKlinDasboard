"""Add private AI memory fields to customers.

Revision ID: 20261020_customer_ai_memory
Revises: p2_task_open_dedup
"""
from alembic import op
import sqlalchemy as sa

revision = "20261020_customer_ai_memory"
down_revision = "p2_task_open_dedup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("ai_memory", sa.Text(), nullable=True))
    op.add_column(
        "customers",
        sa.Column("ai_memory_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("customers", "ai_memory_at")
    op.drop_column("customers", "ai_memory")
