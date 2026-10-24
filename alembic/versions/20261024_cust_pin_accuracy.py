"""Customers: how accurate the saved pin is (metres, from the phone's GPS).

Revision ID: 20261024_cust_pin_accuracy
Revises: 20261024_cust_location_asked
"""

import sqlalchemy as sa
from alembic import op

revision = "20261024_cust_pin_accuracy"
down_revision = "20261024_cust_location_asked"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("location_accuracy_m", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("customers", "location_accuracy_m")
