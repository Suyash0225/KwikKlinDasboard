"""users aur invites par bhi RLS — aakhri do tenant tables.

ARCHITECTURE.md mein likha tha "users/invites par app-level filter hi hai
— RLS nahi lag sakta", bina wajah ke. Lagane par 31 test toote, aur wajah
asli nikli — par woh "nahi lag sakta" nahi, "pehle account lifecycle ka
context theek karo" thi.

DIKKAT KYA THI:

`tenant_scope` middleware bina session wali har request ko HOME tenant ka
context deta hai. Account lifecycle ke paanch kaam is niyam mein fit hi
nahi hote, kyunki unmein dukaan pata hi nahi hoti:

    signup          nayi dukaan BANATA hai — pehla user kis tenant ka?
    login           user milne se PEHLE pata nahi kis dukaan ka hai
    google callback wahi baat, google_sub se
    invite accept   kis dukaan ka invite hai ye TOKEN batata hai
    session adopt   impersonation link kisi bhi dukaan ka ho sakta hai

Inpar RLS lagte hi doosri dukaan ka owner apne hi account se login nahi kar
pata — 401, bina kisi wajah ke. Signup ka pehla user WITH CHECK par phat
jaata (row ka tenant naya, GUC ka home).

FIX schema mein nahi, un paanch jagah par hai: sab ab
`tenant_context.system_context()` mein chalte hain — wahi raasta jo
/control aur razorpay webhook pehle se use karte hain, kyunki ye bhi
platform-level kaam hain, kisi ek dukaan ke andar ka nahi.

Uske baad poori suite green hai (603), isliye ab policy lagayi ja sakti
hai. Ye migration us kaam ke BAAD aati hai, pehle nahi — ulta kram login
tod deta.
"""

from alembic import op

revision = "x8a5c3d9f6b4"
down_revision = "w7f4b2c8e5a3"
branch_labels = None
depends_on = None

TABLES = ("users", "invites")

_PREDICATE = (
    "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
    "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
        )


def downgrade() -> None:
    for t in reversed(TABLES):
        op.execute(f"DROP POLICY tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
