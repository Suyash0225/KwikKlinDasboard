"""Add smart-send campaign metadata and per-campaign rule flags.
Revision ID: 20261019_campaign_smart_send
Revises: 20261018_merge_outbound_heads, 41341a2e745e
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision: str = "20261019_campaign_smart_send"
down_revision: Union[str, Sequence[str], None] = ("20261018_merge_outbound_heads", "41341a2e745e")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
def upgrade() -> None:
    op.add_column("campaigns",sa.Column("campaign_type",sa.String(length=24),server_default="promotion",nullable=False))
    op.add_column("campaigns",sa.Column("rule_flags",postgresql.JSONB(astext_type=sa.Text()),nullable=True))
    op.add_column("campaigns",sa.Column("override_confirmed_by",sa.String(length=80),nullable=True))
    op.add_column("campaigns",sa.Column("override_confirmed_at",sa.DateTime(timezone=True),nullable=True))
    op.create_index("ix_campaigns_campaign_type","campaigns",["campaign_type"],unique=False)
def downgrade() -> None:
    op.drop_index("ix_campaigns_campaign_type",table_name="campaigns")
    op.drop_column("campaigns","override_confirmed_at")
    op.drop_column("campaigns","override_confirmed_by")
    op.drop_column("campaigns","rule_flags")
    op.drop_column("campaigns","campaign_type")
