"""Add per-staff AI WhatsApp switch."""

from alembic import op
import sqlalchemy as sa

revision = "c4d8e1f7a2b9"
down_revision = "merge_20261007_gemini_head"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "staff",
        sa.Column("ai_agent_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("staff", "ai_agent_enabled")
