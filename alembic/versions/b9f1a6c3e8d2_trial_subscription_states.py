"""Trial/subscription states: legacy 'read_only' ko 'past_due' mein merge.

Naya lifecycle (app/models/tenant.py):
    trial -> active -> past_due (READ-ONLY, 30-din grace) -> locked

'read_only' retire ho gaya — uski jagah past_due hi read-only grace hai.
'locked' naya terminal-ish state hai (data safe, dashboard band, payment
aate hi wapas active). tenants.status VARCHAR hai (enum nahi), isliye ye
sirf data migration hai — koi DDL nahi.

Downgrade note: purane model mein past_due (writable) aur read_only alag
the; merge ke baad wo distinction wapas nahi laayi ja sakti. Downgrade
conservative hai: sab past_due -> read_only (zyada restrictive = safe),
aur locked -> read_only (purane code mein locked exist hi nahi karta).

Revision ID: b9f1a6c3e8d2
Revises: d4c8e2f7a915
Create Date: 2026-08-08

"""

from alembic import op

revision = "b9f1a6c3e8d2"
down_revision = "d4c8e2f7a915"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE tenants SET status = 'past_due' WHERE status = 'read_only'")


def downgrade() -> None:
    op.execute(
        "UPDATE tenants SET status = 'read_only' WHERE status IN ('past_due', 'locked')"
    )
