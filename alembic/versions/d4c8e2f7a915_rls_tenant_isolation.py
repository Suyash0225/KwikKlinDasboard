"""Row-Level Security: DB-level tenant isolation on all 19 tenant tables.

Har table par:
  ENABLE + FORCE ROW LEVEL SECURITY   (FORCE zaroori hai — app table-owner
                                       `laundry` se connect karti hai, aur
                                       owner par RLS by default lagti nahi)
  POLICY tenant_isolation:
      app.tenant_id GUC set     -> sirf usi tenant ke rows (read AUR write)
      app.tenant_id GUC unset   -> system context, sab rows
                                   (scheduler, migrations, pg_dump)

GUC ko kaun set karta hai: app/database.py ka "begin" event, har transaction
par, app/services/tenant_context.py ke request context se — jo main.py ka
middleware HAR HTTP request par set karta hai. Matlab: HTTP se aayi koi bhi
query — ORM ho ya raw SQL, endpoint filter kare ya bhool jaye — Postgres khud
doosre tenant ka data dega hi nahi.

System-context arm (GUC unset => full access) DELIBERATE hai, shortcut nahi:
- scheduler/webhook-replay in-process trusted code hai, kisi tenant-user ke
  behalf par nahi chalta; use cross-thread follow-ups bhejne hote hain
- alembic migrations aur pg_dump (jo ab --enable-row-security ke saath
  chalta hai, dekho app/services/backup.py) ko poora data chahiye
- HTTP surface par ye arm kabhi active nahi hota (middleware guarantee)

NULLIF('' ) guard: kisi driver/pooler ne GUC ko empty string chhod diya to
''::uuid ka cast error na aaye — empty ko unset jaisa treat karo.

Reversible: downgrade policies drop + RLS disable kar deta hai.

Revision ID: d4c8e2f7a915
Revises: f0a7b3c9d1e4
Create Date: 2026-08-08

"""

from alembic import op

revision = "d4c8e2f7a915"
down_revision = "f0a7b3c9d1e4"
branch_labels = None
depends_on = None

# Same 19 tables as the foundation migration (f0a7b3c9d1e4).
TENANT_TABLES = [
    "customers",
    "staff",
    "orders",
    "payments",
    "expenses",
    "rate_card",
    "conversations",
    "escalations",
    "tasks",
    "leads",
    "campaigns",
    "coupons",
    "faq_entries",
    "corrections",
    "doc_chunks",
    "open_questions",
    "settings_kv",
    "audit_log",
    "llm_usage",
]

_PREDICATE = (
    "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
    "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    for t in TENANT_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
        )


def downgrade() -> None:
    for t in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
