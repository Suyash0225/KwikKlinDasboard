"""Merge customer AI memory and rate unit cost migration heads.

Revision ID: 20261020_merge_customer_memory_rate_cost
Revises: 20261020_customer_ai_memory, 20261020_rate_unit_cost
"""
from typing import Sequence, Union

revision: str = "20261020_merge_customer_memory_rate_cost"
down_revision: Union[str, Sequence[str], None] = (
    "20261020_customer_ai_memory",
    "20261020_rate_unit_cost",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Merge revision only; both parent migrations perform their own schema changes.
    pass


def downgrade() -> None:
    # Merge revision only; downgrade the parent migrations separately as needed.
    pass
