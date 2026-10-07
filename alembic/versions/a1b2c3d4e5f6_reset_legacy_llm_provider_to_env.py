"""Remove the legacy runtime LLM provider override.

The application now uses paid Gemini only, so old provider-selection rows
must not survive the migration and override the deployment architecture.
"""

from alembic import op


revision = "a1b2c3d4e5f6"
down_revision = "z5a2c7e9f1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM settings_kv
        WHERE key = 'llm_provider'
        """
    )


def downgrade() -> None:
    # Do not recreate the removed provider override.
    pass
