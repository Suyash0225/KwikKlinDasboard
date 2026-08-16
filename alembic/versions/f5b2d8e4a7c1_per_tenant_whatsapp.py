"""Per-tenant WhatsApp: har tenant apna number (official Meta Cloud API).

tenants par teen naye columns:
- wa_phone_number_id (UNIQUE) — inbound webhook isi se tenant par route hota
  hai (value.metadata.phone_number_id), outbound isi number se jaata hai
- wa_waba_id — us tenant ka WhatsApp Business Account id
- wa_token — us WABA ka system-user access token (API mein hamesha masked)

Khali columns = .env wale creds (home shop ka legacy single-tenant path) —
kuch nahi badalta jab tak tenant apna number connect nahi karta.

Reversible: downgrade columns gira deta hai.

Revision ID: f5b2d8e4a7c1
Revises: a2e5c8b1f7d3
Create Date: 2026-08-08

"""

import sqlalchemy as sa
from alembic import op

revision = "f5b2d8e4a7c1"
down_revision = "a2e5c8b1f7d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("wa_phone_number_id", sa.String(30), nullable=True))
    op.add_column("tenants", sa.Column("wa_waba_id", sa.String(30), nullable=True))
    op.add_column("tenants", sa.Column("wa_token", sa.Text, nullable=True))
    op.create_unique_constraint(
        "uq_tenants_wa_phone_number_id", "tenants", ["wa_phone_number_id"]
    )
    op.create_index("ix_tenants_wa_phone_number_id", "tenants", ["wa_phone_number_id"])


def downgrade() -> None:
    op.drop_index("ix_tenants_wa_phone_number_id", table_name="tenants")
    op.drop_constraint("uq_tenants_wa_phone_number_id", "tenants", type_="unique")
    op.drop_column("tenants", "wa_token")
    op.drop_column("tenants", "wa_waba_id")
    op.drop_column("tenants", "wa_phone_number_id")
