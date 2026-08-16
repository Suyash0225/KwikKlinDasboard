"""Recharge/credits: plan limit khatam hone ke baad ka top-up.

tenants.ai_credits / wa_credits  — bacha hua balance (fast read, quota
                                   hot-path yahi padta hai)
credit_ledger                    — har recharge/deduction ka record
                                   (kab, kitna, kisne, kyun) — paisa ka
                                   trail kabhi guess se nahi chalta

Model: plan/override limit poori hone ke BAAD har extra message/AI call
ek credit khaata hai. Credits khatam -> wahi purana QuotaExceeded.

Revision ID: j3e8a5c1d7f2
Revises: i2d7f4b8c3e1
Create Date: 2026-08-09

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "j3e8a5c1d7f2"
down_revision = "i2d7f4b8c3e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column(
        "ai_credits", sa.Integer, nullable=False, server_default="0"))
    op.add_column("tenants", sa.Column(
        "wa_credits", sa.Integer, nullable=False, server_default="0"))
    op.create_table(
        "credit_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", name="fk_credit_ledger_tenant_id_tenants",
                          ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(4), nullable=False),          # ai | wa
        sa.Column("amount", sa.Integer, nullable=False),          # + top-up, - removal
        sa.Column("balance_after", sa.Integer, nullable=False),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_credit_ledger_tenant_id", "credit_ledger", ["tenant_id"])
    op.create_index("ix_credit_ledger_at", "credit_ledger", ["at"])


def downgrade() -> None:
    op.drop_table("credit_ledger")
    op.drop_column("tenants", "wa_credits")
    op.drop_column("tenants", "ai_credits")
