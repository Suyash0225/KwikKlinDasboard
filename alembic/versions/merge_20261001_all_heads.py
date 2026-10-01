"""Merge the active Alembic migration branches.

This is a pure Alembic graph merge. It performs no schema/data changes.
It joins the three historical heads so production and fresh databases have
one deterministic migration head.

Revision ID: merge_20261001_all_heads
Revises: m5a7c9e2b4d1, t4f8c2a9d1e6, z5a2c7e9f1b3
"""

from alembic import op


revision = "merge_20261001_all_heads"
down_revision = ("m5a7c9e2b4d1", "t4f8c2a9d1e6", "z5a2c7e9f1b3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the migration branches without changing the schema."""
    pass


def downgrade() -> None:
    """Split back to the three historical heads."""
    pass
