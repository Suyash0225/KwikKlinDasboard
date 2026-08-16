"""Agent pause ab hamesha ke liye nahi — apne aap wapas chalu hota hai.

Problem: COMPLAINT ya bura rating aane par agent us thread par khud ko
pause kar leta tha (sahi hai — gusse wale customer ko insaan handle kare),
LEKIN wapas chalu karne ka koi rasta nahi tha. Owner Inbox mein resume
karna bhool jaye to wo customer HAMESHA ke liye bina jawab ke reh jaata —
aur agle din uska normal sawal ("shop kab khulegi") bhi anjaana chala
jaata tha.

customers.agent_paused_at — pause kab laga. Naya inbound message aane par,
agar pause ko `agent_pause_hours` (default 24) se zyada ho gaya hai, bot
khud resume kar leta hai. Owner ka manual "Take over" bhi isi tarah expire
hota hai — jaan-boojh kar, kyunki chup rehna sabse mehnga bug hai.

Revision ID: m6b3d9a2c4f8
Revises: l5a2c8f4b9e7
Create Date: 2026-08-09

"""

import sqlalchemy as sa
from alembic import op

revision = "m6b3d9a2c4f8"
down_revision = "l5a2c8f4b9e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("agent_paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Jo abhi paused hain unhe "abhi paused hua" maan lo, warna migration ke
    # turant baad sab ek saath resume ho jaate.
    op.execute(
        "UPDATE customers SET agent_paused_at = now() "
        "WHERE agent_paused = true AND agent_paused_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("customers", "agent_paused_at")
