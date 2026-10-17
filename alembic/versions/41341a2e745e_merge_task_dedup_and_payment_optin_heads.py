"""merge task dedup and payment optin heads

Revision ID: 41341a2e745e
Revises: d9e4f6a1b2c3, p2_task_open_dedup
Create Date: 2026-10-17 11:56:17.933744

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '41341a2e745e'
down_revision: Union[str, Sequence[str], None] = ('d9e4f6a1b2c3', 'p2_task_open_dedup')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
