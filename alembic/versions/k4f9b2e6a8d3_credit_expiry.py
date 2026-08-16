"""Credits ki validity: recharge "X din tak" bhi bech sakein.

credit_ledger par do naye column:
  expires_at — is top-up ki validity (NULL = kabhi khatam nahi, default)
  expired_at — sweep ne ise process kar liya (dobara na kaate)

Balance wahi tenants.ai_credits/wa_credits rehta hai (quota hot-path fast).
Nightly sweep expired lots ka bacha hua hissa balance se kaat deta hai aur
ledger mein ek minus entry likh deta hai — trail poora rehta hai.

Revision ID: k4f9b2e6a8d3
Revises: j3e8a5c1d7f2
Create Date: 2026-08-09

"""

import sqlalchemy as sa
from alembic import op

revision = "k4f9b2e6a8d3"
down_revision = "j3e8a5c1d7f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("credit_ledger",
                  sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("credit_ledger",
                  sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_credit_ledger_expires_at", "credit_ledger", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_credit_ledger_expires_at", table_name="credit_ledger")
    op.drop_column("credit_ledger", "expired_at")
    op.drop_column("credit_ledger", "expires_at")
