"""Search + scale indexes: har badi list ko tenant ke hisaab se tez karo.

Ab tak ke index ek dukaan ke liye bane the. `ix_orders_created_at` jaisa
index SAARI dukaanon ki rows ek saath sajaata hai, isliye jaise-jaise
dukaanein badhengi, ek dukaan ka "aaj ke order" nikalne mein bhi Postgres
doosron ki rows padhkar phenkta rahega. Tees-pachaas dukaanon par yahi
sabse pehle chubhta hai.

Isliye har badi list ka index ab (tenant_id, <sort/filter column>) hai —
tenant pehle. Isse ek dukaan ka data index mein ek jagah baith jaata hai
aur baaki dukaanein raste se hat jaati hain.

Do khaas cheezein:

* **Naam se dhoondhna** (`ILIKE '%anmol%'`) kisi bhi btree index se tez
  nahi hota — beech se match hota hai. Uske liye pg_trgm ka GIN index
  chahiye. pg_trgm PostgreSQL 13+ mein "trusted" hai, yani database owner
  bhi bana sakta hai; phir bhi kisi jagah ijazat na mile to hum ruकte
  nahi — search tab bhi SAHI chalega, bas dheema. Isliye wo ek hissa
  guarded hai, baaki sab pakka.
* Purane single-column index jaan-boojh kar nahi hataye. Wo abhi bhi
  doosre raston (jaise akela order_number lookup) par kaam aate hain, aur
  index girana ek aisa kaam hai jo galat nikle to mehnga padta hai.

Revision ID: q1f7b4e2a8d6
Revises: p9e6a3d1f7c4
"""

import sqlalchemy as sa
from alembic import op

revision = "q1f7b4e2a8d6"
down_revision = "p9e6a3d1f7c4"
branch_labels = None
depends_on = None


# (index ka naam, table, columns-ka-SQL)
_COMPOSITE = [
    # Customers list: "haal hi mein baat ki" ke hisaab se sajti hai
    ("ix_customers_tenant_recent", "customers", "(tenant_id, last_message_at DESC NULLS LAST)"),
    # Number se dhoondhna — aage se match par index chalta hai
    ("ix_customers_tenant_phone_prefix", "customers", "(tenant_id, phone varchar_pattern_ops)"),
    # Bills/orders ki list aur dashboard
    ("ix_orders_tenant_created", "orders", "(tenant_id, created_at DESC)"),
    ("ix_orders_tenant_status", "orders", "(tenant_id, status)"),
    ("ix_orders_tenant_customer", "orders", "(tenant_id, customer_id)"),
    # Tasks: "mera pending kaam" sabse zyada chalne wali query hai
    ("ix_tasks_tenant_status_staff", "tasks", "(tenant_id, status, assigned_staff_id)"),
    ("ix_tasks_tenant_created", "tasks", "(tenant_id, created_at DESC)"),
    # Inbox
    ("ix_conversations_tenant_created", "conversations", "(tenant_id, created_at DESC)"),
    # Paisa: aaj kitna aaya
    ("ix_payments_tenant_received", "payments", "(tenant_id, received_at DESC)"),
]


def upgrade() -> None:
    for name, table, cols in _COMPOSITE:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {cols}")

    # Naam se dhoondhna. Ijazat na mile to search BAND nahi hoti — bas bina
    # index ke chalti hai, isliye yahan poori migration girana galat hoga.
    #
    # SAVEPOINT zaroori hai: Postgres mein ek fail hua statement poore
    # transaction ko abort kar deta hai, to bina savepoint ke iske baad ki
    # har cheez bhi fail hoti. begin_nested() wahi savepoint deta hai.
    conn = op.get_bind()
    try:
        with conn.begin_nested():
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            # Index SEEDHE column par, lower(name) par nahi. Query ILIKE
            # karti hai, aur pg_trgm ILIKE ko is index se chala leta hai;
            # lower() wale expression index par wahi ILIKE match hi nahi
            # karti — index banta, par kabhi istemaal na hota.
            conn.execute(
                sa.text(
                    "CREATE INDEX IF NOT EXISTS ix_customers_name_trgm "
                    "ON customers USING gin (name gin_trgm_ops)"
                )
            )
    except Exception as exc:      # noqa: BLE001 - ye rukne wali baat nahi hai
        print(
            f"  [skip] pg_trgm nahi bana: {exc}\n"
            "  Naam se customer search phir bhi SAHI chalegi, bas badi list par dheemi.\n"
            "  Theek karne ke liye ek baar superuser se: CREATE EXTENSION pg_trgm;"
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_customers_name_trgm")
    for name, _table, _cols in _COMPOSITE:
        op.execute(f"DROP INDEX IF EXISTS {name}")
