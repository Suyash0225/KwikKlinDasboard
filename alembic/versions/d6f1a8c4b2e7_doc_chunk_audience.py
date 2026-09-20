"""Add customer/staff audience to uploaded knowledge documents.

Revision ID: d6f1a8c4b2e7
Revises: z3e9a7b4c1d8
"""
from alembic import op
import sqlalchemy as sa

revision = "d6f1a8c4b2e7"
down_revision = "z3e9a7b4c1d8"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column(
        "doc_chunks",
        sa.Column("audience", sa.String(length=12), nullable=False, server_default="customer"),
    )
    op.create_check_constraint(
        "ck_doc_chunks_audience",
        "doc_chunks",
        "audience IN ('customer', 'staff', 'all')",
    )

def downgrade() -> None:
    op.drop_constraint("ck_doc_chunks_audience", "doc_chunks", type_="check")
    op.drop_column("doc_chunks", "audience")
