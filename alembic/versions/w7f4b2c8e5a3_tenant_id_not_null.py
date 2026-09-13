"""tenant_id ab NOT NULL — un 20 tables par jinka har row kisi dukaan ka hai.

KYUN ZAROORI HAI (aur ye sirf saf-safai nahi hai):

RLS ka predicate `tenant_id = current_setting('app.tenant_id')` hai. NULL
ki kisi bhi cheez se tulna NULL deti hai, TRUE nahi — yaani NULL tenant_id
wali row KISI KO nahi dikhti. Wo delete nahi hoti, error nahi deti, bas
gayab ho jaati hai. `scripts/_bootstrap.py` ka poora docstring isi ek bug
par likha gaya hai: seed se banaya staff dukaan ko dikhta hi nahi tha,
staff login fail hota tha, control panel "0 staff" bolta tha — aur script
ne "staff_row" log kar diya tha, isliye sab kuch chala hua lagta tha.

`before_flush` hook ise rokta hai, par wo aakhri parat nahi honi chahiye:
wo sirf ORM ke raaste par lagta hai, aur cache khali ho to chup-chaap
chhod deta hai. Constraint DB par hai, isliye kisi bhi raaste se aane
wali NULL ko rokta hai.

DO TABLES JAAN-BOOJH KAR CHHODI HAIN — dono par NULL ek asli haalat hai:

billing_events. Razorpay ka webhook kabhi kisi dukaan se map nahi ho paata
(`billing.py`: `tenant_id=tenant.id if tenant else None`). Uspar NOT NULL
lagane ka matlab hai wo INSERT phatega, handler error dega, aur Razorpay
retry karta rahega — yaani ek anmapped event ko record karne ke bajaye hum
use kho denge. Pehle ye list mein tha; grep se pakda.

audit_log. Uski rows platform-level bhi hoti
hain (control panel, system jobs) jinka koi tenant hota hi nahi — 6000 se
zyada aisi rows abhi maujood hain. Uspar NOT NULL lagana asli data ko
jhooth bolne par majboor karta.

PURANE DATA PAR: migration pehle GINTI karta hai aur NULL milne par kuch
badle bina ruk jaata hai, poori list ke saath. Aadha laga hua schema
chhodne se bura kuch nahi, aur "kaunsi row kis dukaan ki thi" ka faisla
migration ka nahi, insaan ka hai.
"""

import sqlalchemy as sa
from alembic import op

revision = "w7f4b2c8e5a3"
down_revision = "v6e3a9b7d4f2"
branch_labels = None
depends_on = None

TABLES = [
    "campaigns", "conversations", "corrections", "coupons",
    "customers", "doc_chunks", "escalations", "expenses", "faq_entries",
    "leads", "llm_usage", "open_questions", "orders", "payments",
    "rate_card", "settings_kv", "staff", "task_messages", "tasks",
]


def upgrade() -> None:
    conn = op.get_bind()

    # Pehle poori jaanch, phir ek bhi ALTER. Table-dar-table chalne par
    # dasvi table par rukne se schema aadha naya aadha purana reh jaata.
    dirty = []
    for t in TABLES:
        n = conn.execute(
            sa.text(f"SELECT count(*) FROM {t} WHERE tenant_id IS NULL")  # noqa: S608
        ).scalar_one()
        if n:
            dirty.append(f"{t}: {n}")
    if dirty:
        raise RuntimeError(
            "tenant_id NULL wali rows maujood hain — inhe pehle theek karo:\n  "
            + "\n  ".join(dirty)
            + "\n\nHar row ka sahi tenant kaunsa hai, ye migration nahi jaan "
            "sakta. Dekh kar UPDATE karo (ya soch-samajh kar DELETE), phir "
            "dobara chalao."
        )

    for t in TABLES:
        op.alter_column(t, "tenant_id", nullable=False)


def downgrade() -> None:
    for t in reversed(TABLES):
        op.alter_column(t, "tenant_id", nullable=True)
