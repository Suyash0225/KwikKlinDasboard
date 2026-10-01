"""add due_at to tasks

Revision ID: t4f8c2a9d1e6
Revises: z3e9a7b4c1d8
"""
from alembic import op
import sqlalchemy as sa

revision = "t4f8c2a9d1e6"
down_revision = "z3e9a7b4c1d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("customer_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_tasks_customer_id", "tasks", "customers", ["customer_id"], ["id"])
    op.create_index("ix_tasks_customer_id", "tasks", ["customer_id"])
    op.add_column("tasks", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tasks_due_at", "tasks", ["due_at"])


def downgrade() -> None:
    op.drop_index("ix_tasks_due_at", table_name="tasks")
    op.drop_column("tasks", "due_at")
