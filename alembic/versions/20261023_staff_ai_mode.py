"""Staff: AI mode dropdown (full / half / none) instead of the on/off toggle.

Purana "AI WhatsApp ON" staff "full" banta hai — kisi ka anubhav achanak
nahi badalta; "OFF" wala "none". Naye staff ORM default se "half" (sasta)
lete hain. ai_agent_enabled wahi rehta hai (= ai_mode != 'none').

Revision ID: 20261023_staff_ai_mode
Revises: 20261022_escalation_status_index
"""

import sqlalchemy as sa
from alembic import op

revision = "20261023_staff_ai_mode"
down_revision = "20261022_escalation_status_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "staff",
        sa.Column("ai_mode", sa.String(5), nullable=False, server_default="full"),
    )
    # staff par RLS fail-closed hai (20261017_rls_fail_closed): bina system
    # context ke ye UPDATE chupchaap 0 rows chhoota aur "OFF" wale staff
    # "full" ban jaate. Sirf is transaction ke liye.
    op.execute("SELECT set_config('app.system_context', 'on', true)")
    op.execute("UPDATE staff SET ai_mode = 'none' WHERE ai_agent_enabled IS FALSE")
    op.create_check_constraint(
        "ck_staff_ai_mode", "staff", "ai_mode IN ('full', 'half', 'none')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_staff_ai_mode", "staff", type_="check")
    op.drop_column("staff", "ai_mode")
