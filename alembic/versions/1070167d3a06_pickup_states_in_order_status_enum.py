"""pickup states in order_status enum

Revision ID: 1070167d3a06
Revises: e3954f7423ce
Create Date: 2026-08-03 23:13:23.174915

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1070167d3a06'
down_revision: Union[str, Sequence[str], None] = 'e3954f7423ce'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add home-pickup workflow states (owner's Order Agent spec).

    ALTER TYPE ... ADD VALUE can't run inside a transaction — autocommit.
    """
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE order_status ADD VALUE IF NOT EXISTS "
            "'PICKUP_ASSIGNED' AFTER 'RECEIVED'"
        )
        op.execute(
            "ALTER TYPE order_status ADD VALUE IF NOT EXISTS "
            "'PICKED_UP' AFTER 'PICKUP_ASSIGNED'"
        )


def downgrade() -> None:
    """Postgres can't drop enum values — accepted one-way door."""
    pass
