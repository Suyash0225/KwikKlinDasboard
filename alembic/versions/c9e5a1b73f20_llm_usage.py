"""per-call LLM usage, for the cost dashboard

Revision ID: c9e5a1b73f20
Revises: b8d42e015c77
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "c9e5a1b73f20"
down_revision = "b8d42e015c77"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(60), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False, server_default="other"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.create_index("ix_llm_usage_at", "llm_usage", ["at"])
    op.create_index("ix_llm_usage_provider", "llm_usage", ["provider"])
    op.create_index("ix_llm_usage_model", "llm_usage", ["model"])
    op.create_index("ix_llm_usage_purpose", "llm_usage", ["purpose"])


def downgrade() -> None:
    op.drop_table("llm_usage")
