"""Self-serve gate: har tenant apna dashboard — do global uniques toot-te the.

1. settings_kv: PK `key` tha (globally unique) — doosra tenant apni setting
   save karte hi PK clash. Ab surrogate id PK + UNIQUE (tenant_id, key)
   [NULLS NOT DISTINCT] — har tenant ki apni settings, reads pehle se
   RLS/ctx-scoped the.
2. orders: order_number globally unique tha — do tenants ek din mein apna
   pehla order banate to doosre ka "KK-YYYYMMDD-01" clash karta (numbering
   ctx-scoped hai). Ab UNIQUE (tenant_id, order_number); order_number par
   plain index lookup ke liye.

Reversible (downgrade tabhi chalega jab data phir single-tenant jaisa ho —
warna purani global uniques wapas lagte hi conflict correctly fail hoga).

Revision ID: h1c5f9e3b7a2
Revises: a9d2f6c4e8b1
Create Date: 2026-08-09

"""

from alembic import op

revision = "h1c5f9e3b7a2"
down_revision = "a9d2f6c4e8b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- settings_kv: per-tenant keys ---
    op.execute(
        "ALTER TABLE settings_kv ADD COLUMN id uuid NOT NULL DEFAULT gen_random_uuid()"
    )
    op.execute("ALTER TABLE settings_kv DROP CONSTRAINT pk_settings_kv")
    op.execute("ALTER TABLE settings_kv ADD CONSTRAINT pk_settings_kv PRIMARY KEY (id)")
    op.execute(
        "ALTER TABLE settings_kv ADD CONSTRAINT uq_settings_kv_tenant_key "
        "UNIQUE NULLS NOT DISTINCT (tenant_id, key)"
    )
    op.create_index("ix_settings_kv_key", "settings_kv", ["key"])

    # --- orders: per-tenant numbering ---
    op.drop_index("ix_orders_order_number", table_name="orders")
    op.execute(
        "ALTER TABLE orders ADD CONSTRAINT uq_orders_tenant_order_number "
        "UNIQUE NULLS NOT DISTINCT (tenant_id, order_number)"
    )
    op.create_index("ix_orders_order_number", "orders", ["order_number"])


def downgrade() -> None:
    op.drop_index("ix_orders_order_number", table_name="orders")
    op.execute("ALTER TABLE orders DROP CONSTRAINT uq_orders_tenant_order_number")
    op.execute(
        "CREATE UNIQUE INDEX ix_orders_order_number ON orders (order_number)"
    )
    op.drop_index("ix_settings_kv_key", table_name="settings_kv")
    op.execute("ALTER TABLE settings_kv DROP CONSTRAINT uq_settings_kv_tenant_key")
    op.execute("ALTER TABLE settings_kv DROP CONSTRAINT pk_settings_kv")
    op.execute("ALTER TABLE settings_kv ADD CONSTRAINT pk_settings_kv PRIMARY KEY (key)")
    op.execute("ALTER TABLE settings_kv DROP COLUMN id")
