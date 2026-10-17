"""Add hosted Razorpay order-payment links and idempotency reference.

Revision ID: 20261017_order_payment_links
Revises: 20261017_rls_fail_closed
"""
from alembic import op
import sqlalchemy as sa

revision = "20261017_order_payment_links"
down_revision = "20261017_rls_fail_closed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("razorpay_payment_link_id", sa.String(length=80), nullable=True))
    op.add_column("orders", sa.Column("razorpay_payment_link_url", sa.String(length=500), nullable=True))
    op.add_column("orders", sa.Column("razorpay_payment_link_amount", sa.Numeric(10, 2), nullable=True))
    op.add_column("payments", sa.Column("provider_reference", sa.String(length=100), nullable=True))
    op.create_unique_constraint("uq_payments_provider_reference", "payments", ["provider_reference"])


def downgrade() -> None:
    op.drop_constraint("uq_payments_provider_reference", "payments", type_="unique")
    op.drop_column("payments", "provider_reference")
    op.drop_column("orders", "razorpay_payment_link_amount")
    op.drop_column("orders", "razorpay_payment_link_url")
    op.drop_column("orders", "razorpay_payment_link_id")
