"""assigned tasks the agent tracks and follows up on

Revision ID: a7c31f8b2d40
Revises: f2a41c9d7e10
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "a7c31f8b2d40"
down_revision = "f2a41c9d7e10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(12), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "assigned_staff_id", UUID(as_uuid=True), sa.ForeignKey("staff.id"), nullable=True
        ),
        sa.Column("order_id", UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="OPEN"),
        sa.Column("urgent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_by", sa.String(40), nullable=False, server_default="owner"),
        sa.Column("reply", sa.Text(), nullable=True),
        sa.Column("ping_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("uq_tasks_code", "tasks", ["code"], unique=True)
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_assigned_staff_id", "tasks", ["assigned_staff_id"])
    op.create_index("ix_tasks_order_id", "tasks", ["order_id"])
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])


def downgrade() -> None:
    op.drop_table("tasks")
