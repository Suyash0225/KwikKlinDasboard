"""add task control-room duplicate guards

Revision ID: u6c91e2b4f70
Revises: t4f8c2a9d1e6
"""
from alembic import op
import sqlalchemy as sa

revision = "u6c91e2b4f70"
down_revision = "t4f8c2a9d1e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_tasks_open_order_kind",
        "tasks",
        ["tenant_id", "order_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN' AND order_id IS NOT NULL"),
    )
    op.create_index(
        "uq_tasks_open_customer_kind",
        "tasks",
        ["tenant_id", "customer_id", "kind"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'OPEN' AND customer_id IS NOT NULL AND order_id IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_tasks_open_customer_kind", table_name="tasks")
    op.drop_index("uq_tasks_open_order_kind", table_name="tasks")
