"""Make referral issuance/reward markers concurrency-safe.

Revision ID: 20261018_referral_idempotency
Revises: 20261018_outbound_throttle
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261018_referral_idempotency"
down_revision = "20261018_outbound_throttle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("referral_owner_customer_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_campaigns_referral_owner_customer_id_customers",
        "campaigns", "customers", ["referral_owner_customer_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_campaigns_referral_owner_customer_id",
        "campaigns", ["referral_owner_customer_id"],
    )
    op.create_unique_constraint(
        "uq_campaign_referral_owner", "campaigns",
        ["tenant_id", "referral_owner_customer_id"],
    )
    # Only referral reward markers are unique; ordinary reward notes remain free-form.
    op.create_index(
        "uq_customer_rewards_referral_marker",
        "customer_rewards", ["tenant_id", "note"], unique=True,
        postgresql_where=sa.text("note LIKE 'referral:%'"),
    )


def downgrade() -> None:
    op.drop_index("uq_customer_rewards_referral_marker", table_name="customer_rewards")
    op.drop_constraint("uq_campaign_referral_owner", "campaigns", type_="unique")
    op.drop_index("ix_campaigns_referral_owner_customer_id", table_name="campaigns")
    op.drop_constraint(
        "fk_campaigns_referral_owner_customer_id_customers", "campaigns", type_="foreignkey"
    )
    op.drop_column("campaigns", "referral_owner_customer_id")
