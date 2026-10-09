"""Preserve lead campaign attribution through first order conversion.

Revision ID: w8f1a6c3d9e2
Revises: merge_20261007_gemini_head
"""

from alembic import op
import sqlalchemy as sa

revision = "w8f1a6c3d9e2"
down_revision = "merge_20261007_gemini_head"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("source_medium", sa.String(length=100), nullable=True))
    op.add_column("leads", sa.Column("source_campaign", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("acquisition_source", sa.String(length=40), nullable=True))
    op.add_column("orders", sa.Column("acquisition_campaign", sa.String(length=100), nullable=True))
    op.create_index("ix_orders_acquisition_source", "orders", ["acquisition_source"], unique=False)
    op.create_index("ix_orders_acquisition_campaign", "orders", ["acquisition_campaign"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_orders_acquisition_campaign", table_name="orders")
    op.drop_index("ix_orders_acquisition_source", table_name="orders")
    op.drop_column("orders", "acquisition_campaign")
    op.drop_column("orders", "acquisition_source")
    op.drop_column("leads", "source_campaign")
    op.drop_column("leads", "source_medium")
