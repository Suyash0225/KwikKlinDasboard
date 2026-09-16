"""orders.bill_seconds — bill banane mein kitne second lage (IMP_006).

"New bill" kholne se Save tak ka samay browser bhejta hai. Isse pata chalta
hai counter par ya darwaze par bill banana kahan atakta hai; reports mein
average aur "slow bills" dikhte hain. Purane orders par NULL.

Revision ID: z1c7e5f1b8d6
Revises: y9b6d4e0a7c5
"""

import sqlalchemy as sa
from alembic import op

revision = "z1c7e5f1b8d6"
down_revision = "y9b6d4e0a7c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("bill_seconds", sa.Integer()))


def downgrade() -> None:
    op.drop_column("orders", "bill_seconds")
