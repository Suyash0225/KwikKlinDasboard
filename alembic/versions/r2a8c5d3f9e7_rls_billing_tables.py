"""RLS on the four billing tables — invoices, billing_events, credit_ledger,
recharge_requests.

Ye chaar tables tenant_id rakhti thin par sirf app-level WHERE se filter
hoti thin — ORM auto-filter bhi nahi (TenantScoped nahi hain), RLS bhi
nahi. Ek WHERE chhoota to dukaan A ko dukaan B ke invoices. Ab wahi policy
jo baaki 19 tables par hai (d4c8e2f7a915):

    app.tenant_id GUC set   -> sirf usi tenant ke rows (read AUR write)
    app.tenant_id GUC unset -> system context, sab rows

Kaun system context mein chalta hai: control panel aur Razorpay webhook
(main.py middleware, isi commit mein) — ye platform ki cheezein hain,
sabki dukaanein dekhni hoti hain. billing_events.tenant_id NULL ho sakta
hai (dukaan delete par detach) — wo rows sirf system ko dikhti hain, jo
sahi hai: kisi dukaan ka nahi raha.

Revision ID: r2a8c5d3f9e7
Revises: q1f7b4e2a8d6
"""

from alembic import op

revision = "r2a8c5d3f9e7"
down_revision = "q1f7b4e2a8d6"
branch_labels = None
depends_on = None

BILLING_TABLES = ["invoices", "billing_events", "credit_ledger", "recharge_requests"]

_PREDICATE = (
    "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
    "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    for t in BILLING_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
        )


def downgrade() -> None:
    for t in reversed(BILLING_TABLES):
        op.execute(f"DROP POLICY tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
