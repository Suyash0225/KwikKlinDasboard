"""orders.assigned_washer_id / assigned_delivery_id: staff hate to NULL.

Ops agent ab har bill par washerman/delivery boy order par laga deta hai.
Staff row delete ho (owner ne hataya, ya purana data saaf) to order us
aadmi par atak kar delete hi rok deta tha. Order ko aadmi ka intezaar nahi
karna — link khali ho jaye, agent agli baar kisi aur ko de dega.

Revision ID: z2d8f6a2c9e7
Revises: z1c7e5f1b8d6
"""

from alembic import op

revision = "z2d8f6a2c9e7"
down_revision = "z1c7e5f1b8d6"
branch_labels = None
depends_on = None

_FKS = (
    ("fk_orders_assigned_washer_id_staff", "assigned_washer_id"),
    ("fk_orders_assigned_delivery_id_staff", "assigned_delivery_id"),
)


def upgrade() -> None:
    for name, col in _FKS:
        op.drop_constraint(name, "orders", type_="foreignkey")
        op.create_foreign_key(name, "orders", "staff", [col], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    for name, col in _FKS:
        op.drop_constraint(name, "orders", type_="foreignkey")
        op.create_foreign_key(name, "orders", "staff", [col], ["id"])
