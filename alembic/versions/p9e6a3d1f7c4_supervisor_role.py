"""Naya role: SUPERVISOR — "Washerman / Manager".

Chhoti dukaan mein aksar sabse senior washerman hi kaam sambhalta hai.
Use sirf "Washerman" likhna galat bhi hai aur bura bhi lagta hai; aur use
poora "Manager" bana dene par uska apna dhulai ka kaam list se gayab ho
jata tha (manager ko sabka kaam dikhta hai, apna alag nahi).

SUPERVISOR dono hai: kaam dhulai wala, taakat manager wali.
"""

from alembic import op

revision = "p9e6a3d1f7c4"
down_revision = "o8d5f2c9e6b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE staff_role ADD VALUE IF NOT EXISTS 'SUPERVISOR'")


def downgrade() -> None:
    # Postgres enum se value hatai nahi ja sakti — SUPERVISOR wale ko
    # WASHER bana dete hain, taaki purana code na phate.
    op.execute("UPDATE staff SET role = 'WASHER' WHERE role = 'SUPERVISOR'")
