"""Customers: Google Maps pin/link; Orders: customer-visible photos.

- customers.lat/lng/map_url/location_at/location_by — pickup/delivery ka
  asli thikana. Staff darwaze par GPS se save karta hai, owner link daalta hai.
- orders.customer_photos — daag/kharabi ki photo jo GRAHAK ko bhi dikhti hai
  (WhatsApp + bill page). orders.notes INTERNAL hai, isliye alag column.

Revision ID: 20261023_customer_location_photos
Revises: 20261023_staff_ai_mode
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261023_customer_location_photos"
down_revision = "20261023_staff_ai_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("lat", sa.Numeric(9, 6), nullable=True))
    op.add_column("customers", sa.Column("lng", sa.Numeric(9, 6), nullable=True))
    op.add_column("customers", sa.Column("map_url", sa.String(500), nullable=True))
    op.add_column("customers", sa.Column("location_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("customers", sa.Column("location_by", sa.String(120), nullable=True))
    op.add_column(
        "orders",
        sa.Column("customer_photos", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "customer_photos")
    for col in ("location_by", "location_at", "map_url", "lng", "lat"):
        op.drop_column("customers", col)
