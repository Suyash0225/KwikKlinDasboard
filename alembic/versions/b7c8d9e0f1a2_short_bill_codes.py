"""Add short customer-facing bill URL codes."""

from alembic import op
import sqlalchemy as sa


revision = "b7c8d9e0f1a2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("bill_code", sa.String(12), nullable=True))
    # Existing orders get a deterministic 12-char code before the unique index.
    # md5 is used only as a compact identifier here, not for authentication.
    op.execute(
        "UPDATE orders SET bill_code = upper(substr(md5(id::text), 1, 12)) "
        "WHERE bill_code IS NULL"
    )
    op.alter_column("orders", "bill_code", nullable=False)
    op.create_index("ix_orders_bill_code", "orders", ["bill_code"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_orders_bill_code", table_name="orders")
    op.drop_column("orders", "bill_code")
