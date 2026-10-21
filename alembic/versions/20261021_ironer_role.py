"""Naya role: IRONER — press/istri wala.

Dhulai ke baad kapde iske paas jaate hain. Kataar washerman ke saath
saanjhi hai: status dono badha sakte hain, task chahe jiske naam ho.

Revision ID: 20261021_ironer_role
Revises: 20261021_staff_points
"""

from alembic import op

revision = "20261021_ironer_role"
down_revision = "20261021_staff_points"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE staff_role ADD VALUE IF NOT EXISTS 'IRONER'")


def downgrade() -> None:
    # Postgres enum se value hatai nahi ja sakti — IRONER ko WASHER bana
    # dete hain, taaki purana code na phate.
    op.execute("UPDATE staff SET role = 'WASHER' WHERE role = 'IRONER'")
