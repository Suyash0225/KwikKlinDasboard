"""Add optional internal unit cost to the tenant rate card.
Revision ID: 20261020_rate_unit_cost
Revises: 20261019_campaign_smart_send
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261020_rate_unit_cost"
down_revision: Union[str, Sequence[str], None] = "20261019_campaign_smart_send"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rate_card", sa.Column("cost_rate", sa.Numeric(10, 2), nullable=True))
    op.create_check_constraint(
        "ck_rate_card_cost_rate_nonnegative", "rate_card",
        "cost_rate IS NULL OR cost_rate >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_rate_card_cost_rate_nonnegative", "rate_card", type_="check")
    op.drop_column("rate_card", "cost_rate")