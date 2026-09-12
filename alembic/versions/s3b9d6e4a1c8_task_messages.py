"""task_messages — staff ke sawaal aur owner ke jawab, ek thread mein.

Pehle "Ask" sirf owner ke WhatsApp par ek line bhejta tha. Kahin store nahi
hota tha, isliye:
- staff ko apna hi sawaal wapas nahi dikhta tha (poocha ya nahi, yaad nahi)
- jawab WhatsApp par aata tha, panel mein kabhi nahi
- owner ke paas ek jagah nahi thi jahan "kis kaam par kya poocha gaya"

Ab dono taraf ek hi thread. Notification badge bhi isi se banta hai:
staff ke liye "mere sawaal ke jawab jo maine nahi padhe".

Revision ID: s3b9d6e4a1c8
Revises: r2a8c5d3f9e7
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "s3b9d6e4a1c8"
down_revision = "r2a8c5d3f9e7"
branch_labels = None
depends_on = None

_PREDICATE = (
    "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
    "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    op.create_table(
        "task_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), index=True),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        # "staff" = kaam karne wala, "owner" = malik/manager ki taraf se
        sa.Column("author_kind", sa.String(10), nullable=False),
        sa.Column("author_name", sa.String(80), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # staff ne padha ya nahi — panel ka badge isi par tika hai
        sa.Column("read_by_staff_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_task_messages_task_at", "task_messages", ["task_id", "at"])
    op.execute("ALTER TABLE task_messages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_messages FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON task_messages "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON task_messages")
    op.drop_index("ix_task_messages_task_at", table_name="task_messages")
    op.drop_table("task_messages")
