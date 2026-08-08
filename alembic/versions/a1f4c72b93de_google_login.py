"""users: auth_provider + google_sub (Google se login)

Revision ID: a1f4c72b93de
Revises: 6e9d756eaef0
Create Date: 2026-08-06

"""

import sqlalchemy as sa
from alembic import op

revision = "a1f4c72b93de"
down_revision = "6e9d756eaef0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "auth_provider", sa.String(length=20),
            server_default="password", nullable=False,
        ),
    )
    op.add_column("users", sa.Column("google_sub", sa.String(length=60), nullable=True))
    op.create_index("ix_users_google_sub", "users", ["google_sub"])


def downgrade() -> None:
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_column("users", "google_sub")
    op.drop_column("users", "auth_provider")
