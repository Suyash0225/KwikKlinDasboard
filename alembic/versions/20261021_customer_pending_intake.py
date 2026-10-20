"""Store the pickup summary the AI asked a customer to confirm.

Revision ID: 20261021_pending_intake
Revises: 20261020_polisher_ledger
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261021_pending_intake"
down_revision = "20261020_polisher_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("ai_pending_intake", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("customers", "ai_pending_intake")
