"""Merge escalation SLA and outbound/referral migration heads.

Revision ID: 20261018_merge_outbound_heads
Revises: 20261017_escalation_sla, 20261018_referral_idempotency
"""
from typing import Sequence, Union

revision: str = "20261018_merge_outbound_heads"
down_revision: Union[str, Sequence[str], None] = (
    "20261017_escalation_sla",
    "20261018_referral_idempotency",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Join independent heads; schema changes are in the parent revisions."""
    pass


def downgrade() -> None:
    """Merge-only revision; downgrade follows Alembic's parent traversal."""
    pass
