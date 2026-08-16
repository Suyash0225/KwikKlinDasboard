"""Staff panel: staff login, MANAGER role, per-tenant phone, cancel approval.

Staff ab tak sirf WhatsApp par the. Ab unka apna panel hai, isliye:

- staff par password (hashed), last_login_at aur must_change_password.
- StaffRole mein MANAGER — wo poore business ka kaam dekhta hai par settings
  aur billing usse door rehte hain.
- phone ki uniqueness ab (tenant_id, phone) par. Pehle poore DB mein ek hi
  baar chal sakta tha — do alag laundry ek hi delivery boy ko nahi rakh
  sakti thi, jo shared multi-tenant mein bilkul galat hai.
- tasks par cancel-request ke teen column: staff cancel MAANG sakta hai,
  kar nahi sakta. Manager approve/reject karta hai.
- staff_sessions: panel ka apna server-side session, jo kabhi bhi maara ja
  sake (phone kho jaye to ek DELETE kaafi ho).

Reversible: downgrade sab kuch wapas le leta hai (MANAGER wale staff ko
WASHER bana kar, kyunki Postgres enum se value hatai nahi ja sakti).
"""

import sqlalchemy as sa
from alembic import op

revision = "n7c4e1b8d5a2"
down_revision = "m6b3d9a2c4f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. MANAGER role. ALTER TYPE ... ADD VALUE transaction ke bahar chahiye.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE staff_role ADD VALUE IF NOT EXISTS 'MANAGER'")

    # 2. Staff ka apna login
    op.add_column("staff", sa.Column("password_hash", sa.String(255), nullable=True))
    op.add_column(
        "staff",
        sa.Column(
            "must_change_password", sa.Boolean(), nullable=False, server_default="true"
        ),
    )
    op.add_column("staff", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))

    # 3. Phone ab per-tenant unique (do businesses ek hi number rakh sakti hain)
    # phone par unique INDEX tha (constraint nahi) — wahi hataana hai.
    # Lookup ke liye non-unique index rehta hai, warna har inbound message
    # ka staff-check sequential scan ban jata.
    op.drop_index("ix_staff_phone", table_name="staff")
    op.create_index("ix_staff_phone", "staff", ["phone"])
    op.create_index(
        "uq_staff_tenant_phone", "staff", ["tenant_id", "phone"], unique=True
    )

    # 4. Cancel maangna — karna nahi
    op.add_column("tasks", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("cancel_requested_by", sa.String(80), nullable=True))
    op.add_column("tasks", sa.Column("cancel_reason", sa.String(300), nullable=True))

    # 5. Panel ka session — server-side, isliye turant maara ja sakta hai
    op.create_table(
        "staff_sessions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "staff_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("staff.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # token kabhi plain text mein nahi — sirf uska sha256
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(200), nullable=True),
    )
    # Baaki tenant tables ki tarah ispar bhi RLS — ek business ka session
    # doosre ko dikh bhi na sake.
    op.execute("ALTER TABLE staff_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE staff_sessions FORCE ROW LEVEL SECURITY")
    # Wahi shakl jo baaki tenant tables par hai (migration d4c8e2f7a915).
    # NULLIF zaroori hai: khali GUC par Postgres cast bhi try kar leta hai
    # aur "invalid input syntax for uuid" de deta — yaani system context
    # (scheduler/backup) mein har query phat jaati.
    policy = (
        "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
        "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY tenant_isolation ON staff_sessions "
        f"USING {policy} WITH CHECK {policy}"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON staff_sessions")
    op.drop_table("staff_sessions")
    op.drop_column("tasks", "cancel_reason")
    op.drop_column("tasks", "cancel_requested_by")
    op.drop_column("tasks", "cancel_requested_at")
    op.drop_index("uq_staff_tenant_phone", table_name="staff")
    op.drop_index("ix_staff_phone", table_name="staff")
    op.create_index("ix_staff_phone", "staff", ["phone"], unique=True)
    op.drop_column("staff", "last_login_at")
    op.drop_column("staff", "must_change_password")
    op.drop_column("staff", "password_hash")
    # Postgres enum se value hatai nahi ja sakti — MANAGER ko WASHER kar dete
    # hain taaki purana code (jo MANAGER nahi jaanta) na phate.
    op.execute("UPDATE staff SET role = 'WASHER' WHERE role = 'MANAGER'")
