"""delivery status + reply link on conversations

The Inbox drew a blue ✓✓ on every outgoing message — it had no idea whether
anything was delivered. Meta tells us (sent/delivered/read/failed); now we
keep it. reply_to_wamid carries a quoted WhatsApp reply.

Revision ID: e5b2c8f1a37d
Revises: d41a7c05b2e9
Create Date: 2026-08-06

"""

import sqlalchemy as sa
from alembic import op

revision = "e5b2c8f1a37d"
down_revision = "d41a7c05b2e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("status", sa.String(length=12), nullable=True))
    op.add_column(
        "conversations", sa.Column("reply_to_wamid", sa.String(length=120), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("conversations", "reply_to_wamid")
    op.drop_column("conversations", "status")
