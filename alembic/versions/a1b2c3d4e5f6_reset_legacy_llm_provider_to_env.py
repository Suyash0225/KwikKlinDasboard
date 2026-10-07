"""Reset legacy OpenRouter provider overrides to the deployment default.

Older deployments may have an explicit llm_provider=openrouter row in
settings_kv. That row overrides LLM_PROVIDER from .env, which makes changing
the deployment provider ineffective.

Only the legacy OpenRouter override is removed. Explicit non-OpenRouter
choices are preserved. With the row removed, app_settings falls back to
DEFAULTS["llm_provider"] == "env", so the deployment's .env provider wins.
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
          AND value->>'v' = 'openrouter'
        """
    )


def downgrade() -> None:
    # Do not recreate an explicit OpenRouter override; the deployment default
    # should remain authoritative after downgrade.
    pass
