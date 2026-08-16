"""Billable meter + client ke recharge requests.

1. conversations.billing_category — 'service' (Meta par FREE), 'utility',
   'marketing'. Ab tak quota SAARE outbound messages ginta tha, jisme wo
   free service replies bhi the — client free cheez par block ho jaata tha
   jabki mehngi cheez (marketing) khuli thi. Ab meter sirf BILLABLE
   (utility + marketing) ginta hai.

2. recharge_requests — client apne billing page se "mujhe 1000 messages
   chahiye" bhejta hai; vendor panel se approve karte hi credits chadh
   jaate hain (paisa GPay/UPI se manually aata hai).

Revision ID: l5a2c8f4b9e7
Revises: k4f9b2e6a8d3
Create Date: 2026-08-09

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "l5a2c8f4b9e7"
down_revision = "k4f9b2e6a8d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("billing_category", sa.String(10), nullable=True),
    )
    # Purana data: template log line se pehchan lo, baaki sab service (free)
    op.execute(
        "UPDATE conversations SET billing_category = CASE "
        "  WHEN direction = 'OUTBOUND' AND message_text LIKE 'Template:%' THEN 'utility' "
        "  WHEN direction = 'OUTBOUND' THEN 'service' ELSE NULL END"
    )
    op.create_index(
        "ix_conversations_billing", "conversations",
        ["tenant_id", "billing_category", "created_at"],
    )

    op.create_table(
        "recharge_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", name="fk_recharge_requests_tenant_id_tenants",
                          ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pack", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(4), nullable=False),        # ai | wa
        sa.Column("units", sa.Integer, nullable=False),
        sa.Column("amount_inr", sa.Integer, nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("note", sa.String(200), nullable=True),       # client ka UTR/ref
        sa.Column("requested_by", sa.String(160), nullable=True),
        sa.Column("decided_by", sa.String(80), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_recharge_requests_tenant_id", "recharge_requests", ["tenant_id"])
    op.create_index("ix_recharge_requests_status", "recharge_requests", ["status"])


def downgrade() -> None:
    op.drop_table("recharge_requests")
    op.drop_index("ix_conversations_billing", table_name="conversations")
    op.drop_column("conversations", "billing_category")
