"""expenses: kisne likha — staff panel se kharcha aane laga.

Pehle kharcha sirf owner dashboard (ya uska AI agent) se aata tha, isliye
"kisne daala" poochhne ki zaroorat nahi thi. Ab delivery wala petrol aur
manager detergent khud likhta hai. Do cheezein chahiye:
- staff ko sirf APNA daala kharcha dikhe (staff_id se)
- owner ko har row par naam dikhe, chahe staff baad mein hata diya jaye
  (added_by — naam ki copy; staff_id delete par NULL ho jaata hai)

Revision ID: y9b6d4e0a7c5
Revises: x8a5c3d9f6b4
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "y9b6d4e0a7c5"
down_revision = "x8a5c3d9f6b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("expenses", sa.Column("added_by", sa.String(120)))
    op.add_column(
        "expenses",
        sa.Column("staff_id", UUID(as_uuid=True), sa.ForeignKey("staff.id", ondelete="SET NULL")),
    )
    op.create_index("ix_expenses_staff_id", "expenses", ["staff_id"])


def downgrade() -> None:
    op.drop_index("ix_expenses_staff_id", table_name="expenses")
    op.drop_column("expenses", "staff_id")
    op.drop_column("expenses", "added_by")
