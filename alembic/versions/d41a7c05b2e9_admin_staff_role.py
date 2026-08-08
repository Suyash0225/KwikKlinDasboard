"""ADMIN value in the staff_role enum

The owner is a person in this system too, not just a phone number in .env:
he needs a staff row so the agent can name him, message him and count him
among the people a customer problem must reach.

Revision ID: d41a7c05b2e9
Revises: c9e5a1b73f20
Create Date: 2026-08-06

"""

from alembic import op

revision = "d41a7c05b2e9"
down_revision = "c9e5a1b73f20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """ALTER TYPE ... ADD VALUE cannot run inside a transaction — autocommit."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE staff_role ADD VALUE IF NOT EXISTS 'ADMIN'")


def downgrade() -> None:
    """Postgres cannot drop enum values — accepted one-way door."""
    pass
