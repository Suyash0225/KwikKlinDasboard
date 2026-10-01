"""Add business attribution to LLM usage rows.

Keeps the existing token/cost history intact while allowing customer-facing
AI calls to be traced to a customer and, when unambiguous, an order.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "m5a7c9e2b4d1"
down_revision = "k4f9b2e6a8d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_usage",
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "llm_usage",
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "llm_usage",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_llm_usage_customer_id",
        "llm_usage",
        "customers",
        ["customer_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_llm_usage_order_id",
        "llm_usage",
        "orders",
        ["order_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_llm_usage_conversation_id",
        "llm_usage",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_llm_usage_customer_id", "llm_usage", ["customer_id"])
    op.create_index("ix_llm_usage_order_id", "llm_usage", ["order_id"])
    op.create_index("ix_llm_usage_conversation_id", "llm_usage", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_conversation_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_order_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_customer_id", table_name="llm_usage")
    op.drop_constraint("fk_llm_usage_conversation_id", "llm_usage", type_="foreignkey")
    op.drop_constraint("fk_llm_usage_order_id", "llm_usage", type_="foreignkey")
    op.drop_constraint("fk_llm_usage_customer_id", "llm_usage", type_="foreignkey")
    op.drop_column("llm_usage", "conversation_id")
    op.drop_column("llm_usage", "order_id")
    op.drop_column("llm_usage", "customer_id")
