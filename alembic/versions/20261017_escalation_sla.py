"""Add owner reminder timestamp to escalations.

Revision ID: 20261017_escalation_sla
Revises: 20261017_order_payment_links
"""
from alembic import op
import sqlalchemy as sa

revision = "20261017_escalation_sla"
down_revision = "20261017_order_payment_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("escalations", sa.Column("owner_reminded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("escalations", "owner_reminded_at")
