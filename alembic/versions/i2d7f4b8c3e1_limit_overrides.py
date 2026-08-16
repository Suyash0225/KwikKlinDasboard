"""Client Ops: per-tenant limit overrides.

tenants.limit_overrides (JSONB) — plan ke defaults ke upar vendor ka haath:
{"ai_usage_limit": 500, "whatsapp_message_limit": 1000, ...}. Khali {} =
plan ke hi limits. Enforcement plans.effective_limits() se hota hai.

Revision ID: i2d7f4b8c3e1
Revises: h1c5f9e3b7a2
Create Date: 2026-08-09

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "i2d7f4b8c3e1"
down_revision = "h1c5f9e3b7a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "limit_overrides", postgresql.JSONB,
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "limit_overrides")
