"""Merge the paid-Gemini reset branch with the canonical migration head.

Revision ID: merge_20261007_gemini_head
Revises: b7c8d9e0f1a2, merge_20261001_all_heads
"""

from alembic import op

revision = "merge_20261007_gemini_head"
down_revision = ("b7c8d9e0f1a2", "merge_20261001_all_heads")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
