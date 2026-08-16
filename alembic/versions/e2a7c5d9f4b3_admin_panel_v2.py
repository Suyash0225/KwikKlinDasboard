"""Admin panel v2: invites (no-plaintext-password onboarding), per-admin
keys with rotation, tenant soft-delete.

- invites:    set-password links — token ka sirf sha256 hash, plaintext
              password kabhi banta/store/bheja nahi jaata
- admin_keys: shared env-key ki jagah per-admin keys (hash-only, level:
              read|write|danger, revoke = rotation)
- tenants.deleted_at: soft delete / recycle bin — list se gayab, data
              salamat; hard purge alag explicit action

Reversible.

Revision ID: e2a7c5d9f4b3
Revises: c6e9d4b2f8a1
Create Date: 2026-08-08

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e2a7c5d9f4b3"
down_revision = "c6e9d4b2f8a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", name="fk_invites_tenant_id_tenants"),
            nullable=False,
        ),
        sa.Column("email", sa.String(160), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="OWNER"),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("invited_by", sa.String(80), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
    )
    op.create_unique_constraint("uq_invites_token_hash", "invites", ["token_hash"])
    op.create_index("ix_invites_token_hash", "invites", ["token_hash"])
    op.create_index("ix_invites_tenant_id", "invites", ["tenant_id"])
    op.create_index("ix_invites_email", "invites", ["email"])
    op.create_index("ix_invites_expires_at", "invites", ["expires_at"])

    op.create_table(
        "admin_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("level", sa.String(10), nullable=False, server_default="write"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_admin_keys_key_hash", "admin_keys", ["key_hash"])
    op.create_index("ix_admin_keys_key_hash", "admin_keys", ["key_hash"])

    op.add_column("tenants", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("billing_cycle", sa.String(10), nullable=False, server_default="monthly"),
    )


def downgrade() -> None:
    op.drop_column("tenants", "billing_cycle")
    op.drop_column("tenants", "deleted_at")
    op.drop_table("admin_keys")
    op.drop_table("invites")
