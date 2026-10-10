"""Add per-staff AI WhatsApp switch."""

from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "d4c8e2f7a915"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "staff",
        sa.Column("ai_agent_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("staff", "ai_agent_enabled")
