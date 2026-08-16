"""invoices: har successful charge ki per-tenant receipt.

billing_events raw webhook journal hai; invoices uski saaf shakal —
kitna paisa, kis plan/cycle ka, kis period ke liye. rzp_payment_id unique
hai taaki payment.captured + subscription.charged (ek hi payment ke do
events) se do receipts na banein. Kabhi delete nahi hoti.

Revision ID: a2e5c8b1f7d3
Revises: b9f1a6c3e8d2
Create Date: 2026-08-08

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a2e5c8b1f7d3"
down_revision = "b9f1a6c3e8d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", name="fk_invoices_tenant_id_tenants"),
            nullable=False,
        ),
        sa.Column("rzp_payment_id", sa.String(60), nullable=False),
        sa.Column("rzp_subscription_id", sa.String(60), nullable=True),
        sa.Column("plan", sa.String(24), nullable=False),
        sa.Column("cycle", sa.String(10), nullable=False),
        sa.Column("amount_paise", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="INR"),
        sa.Column("status", sa.String(12), nullable=False, server_default="paid"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_unique_constraint(
        "uq_invoices_rzp_payment_id", "invoices", ["rzp_payment_id"]
    )
    op.create_index("ix_invoices_tenant_id", "invoices", ["tenant_id"])
    op.create_index("ix_invoices_rzp_subscription_id", "invoices", ["rzp_subscription_id"])
    op.create_index("ix_invoices_created_at", "invoices", ["created_at"])
    op.create_index("ix_invoices_rzp_payment_id", "invoices", ["rzp_payment_id"])


def downgrade() -> None:
    op.drop_table("invoices")
