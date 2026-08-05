"""task kind (pickup) and the ETA the staff member promised

Revision ID: b8d42e015c77
Revises: a7c31f8b2d40
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d42e015c77"
down_revision = "a7c31f8b2d40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("kind", sa.String(12), nullable=False, server_default="general"),
    )
    op.add_column("tasks", sa.Column("eta_text", sa.String(120), nullable=True))
    op.create_index("ix_tasks_kind", "tasks", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_tasks_kind", table_name="tasks")
    op.drop_column("tasks", "eta_text")
    op.drop_column("tasks", "kind")
