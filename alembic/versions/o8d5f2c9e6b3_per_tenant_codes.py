"""Task code aur customer phone ab PER-DUKAAN unique.

Orders pehle hi (tenant_id, order_number) par the — tasks aur customers
chhoot gaye the. Nateeja: doosri dukaan mein pehla hi task banate waqt
"T-1" pehle se maujood milta (kisi aur ki dukaan ka), aur INSERT phat
jaata. Yahi baat customer ke number par bhi lagti hai — do alag laundry
ke paas ek hi grahak ho sakta hai.

Ye ussi niyam ka hissa hai jo orders par pehle se hai: har dukaan ki apni
ginti, apne number.
"""

from alembic import op

revision = "o8d5f2c9e6b3"
down_revision = "n7c4e1b8d5a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLAlchemy ne ise unique INDEX banaya tha, constraint nahi
    op.drop_index("uq_tasks_code", table_name="tasks")
    op.create_index("uq_tasks_tenant_code", "tasks", ["tenant_id", "code"], unique=True)
    # code se lookup ab bhi hota hai (done T-11) — non-unique index rakho
    op.create_index("ix_tasks_code", "tasks", ["code"])

    op.drop_index("ix_customers_phone", table_name="customers")
    op.create_index("ix_customers_phone", "customers", ["phone"])
    op.create_index(
        "uq_customers_tenant_phone", "customers", ["tenant_id", "phone"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_customers_tenant_phone", table_name="customers")
    op.drop_index("ix_customers_phone", table_name="customers")
    op.create_index("ix_customers_phone", "customers", ["phone"], unique=True)

    op.drop_index("ix_tasks_code", table_name="tasks")
    op.drop_index("uq_tasks_tenant_code", table_name="tasks")
    op.create_index("uq_tasks_code", "tasks", ["code"], unique=True)
