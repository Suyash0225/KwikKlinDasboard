"""Add compact payment-offer codes and extend offer duration to 15 minutes."""

from alembic import op
import sqlalchemy as sa


revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payment_offers",
        sa.Column("offer_code", sa.String(12), nullable=True),
    )
    # Existing offers get compact opaque codes before the unique index.
    op.execute(
        "UPDATE payment_offers "
        "SET offer_code = upper(substr(md5(id::text), 1, 12)) "
        "WHERE offer_code IS NULL"
    )
    op.alter_column("payment_offers", "offer_code", nullable=False)
    op.create_index("ix_payment_offers_offer_code", "payment_offers", ["offer_code"], unique=True)
    op.alter_column("payment_offers", "duration_seconds", server_default="900")
    # Unopened offers have not started yet, so they can safely adopt the new duration.
    op.execute(
        "UPDATE payment_offers SET duration_seconds = 900 "
        "WHERE opened_at IS NULL"
    )


def downgrade() -> None:
    op.alter_column("payment_offers", "duration_seconds", server_default="120")
    op.drop_index("ix_payment_offers_offer_code", table_name="payment_offers")
    op.drop_column("payment_offers", "offer_code")
