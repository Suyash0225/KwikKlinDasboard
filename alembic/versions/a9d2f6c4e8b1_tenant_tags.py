"""Phase 2 (client profile): tenants.tags — free-form labels.

["vip", "referral", "slow-payer"] jaise chhote labels — profile page par
chips, list par filter (aage). JSONB list; khali default.

Revision ID: a9d2f6c4e8b1
Revises: f7b1e4d2c9a5
Create Date: 2026-08-08

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a9d2f6c4e8b1"
down_revision = "f7b1e4d2c9a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "tags", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "tags")
