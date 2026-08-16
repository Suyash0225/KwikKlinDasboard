"""Log tables ka tenants-FK: ON DELETE SET NULL.

audit_log aur llm_usage LOGS hain, business data nahi — tenant delete hone
par unki history bachni chahiye (tenant link NULL ho jaata hai; slug waise
bhi args mein hota hai). Pehle RESTRICT tha, jisse:
- /control se tenant delete 500 deta tha jaise hi us tenant ka koi
  login/audit row ban jata (production bug), aur
- har test-cleanup ko audit rows manually ginne padte the.

Business tables (customers/orders/...) RESTRICT par hi hain — unka delete
sochkar, explicitly hota hai.

Revision ID: c6e9d4b2f8a1
Revises: f5b2d8e4a7c1
Create Date: 2026-08-08

"""

from alembic import op

revision = "c6e9d4b2f8a1"
down_revision = "f5b2d8e4a7c1"
branch_labels = None
depends_on = None

LOG_TABLES = ("audit_log", "llm_usage")


def upgrade() -> None:
    for t in LOG_TABLES:
        op.drop_constraint(f"fk_{t}_tenant_id_tenants", t, type_="foreignkey")
        op.create_foreign_key(
            f"fk_{t}_tenant_id_tenants", t, "tenants",
            ["tenant_id"], ["id"], ondelete="SET NULL",
        )


def downgrade() -> None:
    for t in LOG_TABLES:
        op.drop_constraint(f"fk_{t}_tenant_id_tenants", t, type_="foreignkey")
        op.create_foreign_key(
            f"fk_{t}_tenant_id_tenants", t, "tenants", ["tenant_id"], ["id"]
        )
