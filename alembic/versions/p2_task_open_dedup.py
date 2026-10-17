"""Prevent duplicate open tasks for the same order/customer and kind.

Revision ID: p2_task_open_dedup
Revises: merge_20261007_gemini_head
"""
from alembic import op
import sqlalchemy as sa

revision = "p2_task_open_dedup"
down_revision = "merge_20261007_gemini_head"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve the full task history; close only newer duplicate OPEN rows before
    # enforcing uniqueness. Keep the oldest task as the canonical work item.
    op.execute(sa.text("""
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY tenant_id, order_id, kind
                ORDER BY created_at ASC, id ASC
            ) AS rn
            FROM tasks
            WHERE status = 'OPEN' AND order_id IS NOT NULL
        )
        UPDATE tasks
        SET status = 'CANCELLED', completed_at = now()
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
    """))
    op.execute(sa.text("""
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY tenant_id, customer_id, kind
                ORDER BY created_at ASC, id ASC
            ) AS rn
            FROM tasks
            WHERE status = 'OPEN'
              AND customer_id IS NOT NULL
              AND order_id IS NULL
        )
        UPDATE tasks
        SET status = 'CANCELLED', completed_at = now()
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
    """))
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
