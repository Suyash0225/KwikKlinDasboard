"""Customers: when we asked them (once) to share their WhatsApp location.

Revision ID: 20261024_cust_location_asked
Revises: 20261023_cust_location_photos
"""

import sqlalchemy as sa
from alembic import op

revision = "20261024_cust_location_asked"
down_revision = "20261023_cust_location_photos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("location_asked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("customers", "location_asked_at")
