"""Add per-customer automatic payment reminder opt-in."""

from alembic import op
import sqlalchemy as sa

revision = "d9e4f6a1b2c3"
down_revision = ("a6c2f8d1e5b9", "c4d8e1f7a2b9")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column(
            "payment_reminders_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("customers", "payment_reminders_enabled")
