"""durability tables (webhook journal + outbound queue) and hot-path indexes

Revision ID: f2a41c9d7e10
Revises: ba1f7299158e
Create Date: 2026-08-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "f2a41c9d7e10"
down_revision = "ba1f7299158e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "status", sa.String(12), nullable=False, server_default="received"
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_webhook_events_event_key", "webhook_events", ["event_key"], unique=True
    )
    op.create_index("ix_webhook_events_status", "webhook_events", ["status"])
    op.create_index("ix_webhook_events_received_at", "webhook_events", ["received_at"])

    op.create_table(
        "outbound_queue",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("to_phone", sa.String(20), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_outbound_queue_status", "outbound_queue", ["status"])
    op.create_index(
        "ix_outbound_queue_next_attempt_at", "outbound_queue", ["next_attempt_at"]
    )

    # Hot-path indexes the audit found missing.
    op.create_index(
        "ix_conversations_customer_created",
        "conversations",
        ["customer_id", "created_at"],
    )
    op.create_index(
        "ix_conversations_staff_created", "conversations", ["staff_id", "created_at"]
    )
    op.create_index(
        "ix_customers_last_message_at", "customers", ["last_message_at"]
    )
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_created_at", "orders", ["created_at"])
    op.create_index("ix_orders_expected_delivery", "orders", ["expected_delivery"])
    op.create_index(
        "ix_order_status_history_changed_at", "order_status_history", ["changed_at"]
    )
    op.create_index("ix_escalations_customer_id", "escalations", ["customer_id"])
    op.create_index("ix_escalations_order_id", "escalations", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_escalations_order_id", table_name="escalations")
    op.drop_index("ix_escalations_customer_id", table_name="escalations")
    op.drop_index("ix_order_status_history_changed_at", table_name="order_status_history")
    op.drop_index("ix_orders_expected_delivery", table_name="orders")
    op.drop_index("ix_orders_created_at", table_name="orders")
    op.drop_index("ix_orders_status", table_name="orders")
    op.drop_index("ix_customers_last_message_at", table_name="customers")
    op.drop_index("ix_conversations_staff_created", table_name="conversations")
    op.drop_index("ix_conversations_customer_created", table_name="conversations")
    op.drop_table("outbound_queue")
    op.drop_table("webhook_events")
