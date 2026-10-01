"""Merge the active Alembic branches.

This is a graph-only merge revision. It reconciles the three existing heads:
LLM usage attribution, task due_at, and production schema reconciliation.
"""

revision = "u6c3d8f1a2b4"
down_revision = (
    "m5a7c9e2b4d1",
    "t4f8c2a9d1e6",
    "z5a2c7e9f1b3",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
