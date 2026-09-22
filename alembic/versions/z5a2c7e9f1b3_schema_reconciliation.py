"""Reconcile legacy production schema with the current migration line.

Some production databases were created from an older migration branch that added
payment-offer codes and knowledge-document audience fields. The current main
migration line no longer contains those historical revision files, while the
application still supports the corresponding schema.

This migration is intentionally conditional so it can reconcile both fresh
databases and databases coming from the older branch. It also keeps the legacy
orders.bill_code column nullable.
"""

from alembic import op


revision = "z5a2c7e9f1b3"
down_revision = "z4f1b2c8d6a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'orders'
                  AND column_name = 'bill_code'
            ) THEN
                ALTER TABLE orders ALTER COLUMN bill_code DROP NOT NULL;
            END IF;
        END
        $$;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'payment_offers'
            ) THEN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'payment_offers'
                      AND column_name = 'offer_code'
                ) THEN
                    ALTER TABLE payment_offers ADD COLUMN offer_code VARCHAR(12);
                END IF;

                UPDATE payment_offers
                SET offer_code = upper(substr(md5(id::text), 1, 12))
                WHERE offer_code IS NULL;

                ALTER TABLE payment_offers ALTER COLUMN offer_code SET NOT NULL;

                IF NOT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND indexname = 'ix_payment_offers_offer_code'
                ) THEN
                    CREATE UNIQUE INDEX ix_payment_offers_offer_code
                    ON payment_offers (offer_code);
                END IF;

                ALTER TABLE payment_offers
                ALTER COLUMN duration_seconds SET DEFAULT 900;

                UPDATE payment_offers
                SET duration_seconds = 900
                WHERE opened_at IS NULL;
            END IF;
        END
        $$;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'doc_chunks'
            ) THEN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'doc_chunks'
                      AND column_name = 'audience'
                ) THEN
                    ALTER TABLE doc_chunks
                    ADD COLUMN audience VARCHAR(12) NOT NULL DEFAULT 'customer';
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_doc_chunks_audience'
                      AND conrelid = 'public.doc_chunks'::regclass
                ) THEN
                    ALTER TABLE doc_chunks
                    ADD CONSTRAINT ck_doc_chunks_audience
                    CHECK (audience IN ('customer', 'staff', 'all'));
                END IF;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    pass
