"""Escalations: index for the open-escalation lookups.

Dashboard aur SLA reminders `status = OPEN` par chhaante hain — is column
par index hi nahi tha (sirf customer_id/order_id par), har baar poori
table padhi jaati thi.

Revision ID: 20261022_escalation_status_index
Revises: 20261021_ironer_role
"""

from alembic import op

revision = "20261022_escalation_status_index"
down_revision = "20261021_ironer_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_escalations_tenant_status_created",
        "escalations",
        ["tenant_id", "status", "created_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_escalations_tenant_status_created", table_name="escalations", if_exists=True)
