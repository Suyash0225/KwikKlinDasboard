"""Make tenant RLS fail closed unless trusted system context is explicit.

Revision ID: 20261017_rls_fail_closed
Revises: 41341a2e745e
"""

from alembic import op

revision = "20261017_rls_fail_closed"
down_revision = "41341a2e745e"
branch_labels = None
depends_on = None

_FAIL_CLOSED = """(
    COALESCE(current_setting('app.system_context', true), '') = 'on'
    OR (
        NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL
        AND tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    )
)"""

_FAIL_OPEN = """(
    NULLIF(current_setting('app.tenant_id', true), '') IS NULL
    OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
)"""


def _replace_tenant_policies(predicate: str) -> None:
    """Rebuild every tenant_id isolation policy in the current schema."""
    conn = op.get_bind()
    tables = conn.exec_driver_sql(
        """
        SELECT DISTINCT p.schemaname, p.tablename
        FROM pg_policies AS p
        JOIN information_schema.columns AS c
          ON c.table_schema = p.schemaname
         AND c.table_name = p.tablename
         AND c.column_name = 'tenant_id'
        WHERE p.schemaname = current_schema()
          AND p.policyname = 'tenant_isolation'
        ORDER BY p.schemaname, p.tablename
        """
    ).all()
    for schema, table in tables:
        preparer = conn.dialect.identifier_preparer
        qualified = f"{preparer.quote_schema(schema)}.{preparer.quote(table)}"
        op.execute(f"DROP POLICY tenant_isolation ON {qualified}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {qualified} "
            f"USING {predicate} WITH CHECK {predicate}"
        )


def upgrade() -> None:
    _replace_tenant_policies(_FAIL_CLOSED)


def downgrade() -> None:
    _replace_tenant_policies(_FAIL_OPEN)
