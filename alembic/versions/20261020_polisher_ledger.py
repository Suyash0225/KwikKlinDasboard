"""Create external polisher ledger.

Revision ID: 20261020_polisher_ledger
Revises: 20261020_merge_mem_rate
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261020_polisher_ledger"
down_revision = "20261020_merge_mem_rate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "polisher_vendors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("rate_per_saree", sa.Numeric(10, 2), nullable=False, server_default="25.00"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_polisher_vendors_tenant_id", "polisher_vendors", ["tenant_id"])
    op.create_table(
        "polish_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("polisher_vendors.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("batch_code", sa.String(32), nullable=False),
        sa.Column("sent_on", sa.Date(), nullable=False),
        sa.Column("quantity_sent", sa.Integer(), nullable=False),
        sa.Column("quantity_returned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rate_per_saree", sa.Numeric(10, 2), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("last_notification_status", sa.String(24), nullable=False, server_default="not_sent"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_polish_batches_tenant_id", "polish_batches", ["tenant_id"])
    op.create_index("ix_polish_batches_vendor_id", "polish_batches", ["vendor_id"])
    op.create_index("ix_polish_batches_batch_code", "polish_batches", ["batch_code"])
    op.create_index("ix_polish_batches_sent_on", "polish_batches", ["sent_on"])
    op.create_table(
        "polisher_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("polisher_vendors.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("method", sa.String(32), nullable=False, server_default="cash"),
        sa.Column("reference", sa.String(120)),
        sa.Column("notes", sa.Text()),
        sa.Column("notification_status", sa.String(24), nullable=False, server_default="not_sent"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_polisher_payments_tenant_id", "polisher_payments", ["tenant_id"])
    op.create_index("ix_polisher_payments_vendor_id", "polisher_payments", ["vendor_id"])
    op.create_index("ix_polisher_payments_paid_on", "polisher_payments", ["paid_on"])


def downgrade() -> None:
    op.drop_table("polisher_payments")
    op.drop_table("polish_batches")
    op.drop_table("polisher_vendors")
