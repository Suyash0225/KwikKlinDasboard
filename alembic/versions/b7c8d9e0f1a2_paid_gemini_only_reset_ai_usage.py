"""Reset AI usage history and remove legacy LLM provider overrides.

The owner is starting a fresh paid-Gemini usage period. Business data is
untouched: only LLM metering rows and obsolete provider-setting rows are
removed.
"""

from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fresh AI usage ledger — deliberately destructive, per owner request.
    op.execute("DELETE FROM llm_usage")

    # Runtime provider selection is now hard-coded to paid Gemini.
    op.execute(
        """
        DELETE FROM settings_kv
        WHERE key IN ('llm_provider', 'openrouter_api_key')
        """
    )


def downgrade() -> None:
    # Do not restore historical usage or obsolete provider overrides.
    pass
