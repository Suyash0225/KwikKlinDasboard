"""Make legacy orders.bill_code optional.

The current Order model no longer owns bill_code, but some existing databases
still have the legacy column with a NOT NULL constraint. New orders therefore
fail during INSERT even though the application no longer supplies bill_code.

Fresh databases may not have the legacy column at all, so this migration is
intentionally conditional.
"""

from alembic import op

revision = "z4f1b2c8d6a9"
down_revision = "z3e9a7b4c1d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'orders'
                  AND column_name = 'bill_code'
            ) THEN
                ALTER TABLE orders
                ALTER COLUMN bill_code DROP NOT NULL;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'orders'
                  AND column_name = 'bill_code'
            ) THEN
                ALTER TABLE orders
                ALTER COLUMN bill_code SET NOT NULL;
            END IF;
        END
        $$;
        """
    )
