"""coupon_redemptions par bhi RLS aur tenant_id NOT NULL.

u5d2f8a6c3e1 mein is table par `tenant_id` isliye joda gaya tha ki coupon
ka FK ab (tenant_id, code) par hai. Column jud gaya, par do cheezein
adhoori chhoot gayin — meri hi:

1. RLS. Baaki har tenant-scoped table par `tenant_isolation` policy hai;
   ye table un teen mein reh gaya tha jinpar nahi hai (`users` aur
   `invites` ke saath, jinke liye ARCHITECTURE.md mein wajah likhi hai —
   yahan koi wajah nahi thi, sirf chook thi).

2. NOT NULL. Ye column FK ka aadha hissa hai, aur composite FK mein ek
   column NULL ho to Postgres (MATCH SIMPLE) poori jodi ki jaanch chhod
   deta hai. Yaani NULL tenant_id wali redemption kisi bhi coupon se
   "judi" maani jaati — integrity chup-chaap gayab.

Table abhi khaali hai (jaancha), isliye backfill ki zaroorat nahi. Agar
kabhi rows hotin to pehle unhe coupon se backfill karna padta, tabhi
NOT NULL lagta.
"""

from alembic import op

revision = "v6e3a9b7d4f2"
down_revision = "u5d2f8a6c3e1"
branch_labels = None
depends_on = None

_PREDICATE = (
    "(NULLIF(current_setting('app.tenant_id', true), '') IS NULL "
    "OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    # Bachi hui rows ko coupon se bhar do — aaj table khaali hai, par ye
    # migration kal kisi aur DB par bhi chalegi jahan rows ho sakti hain.
    op.execute(
        """
        UPDATE coupon_redemptions r SET tenant_id = c.tenant_id
        FROM coupons c WHERE r.coupon_code = c.code AND r.tenant_id IS NULL
        """
    )
    # Jo phir bhi anaath hain (coupon hi nahi bacha) — unhe rakhna FK tod
    # dega. Ye sirf gande data par hoga, aur chup-chaap chhodne se bura
    # hai saaf hataa dena.
    op.execute("DELETE FROM coupon_redemptions WHERE tenant_id IS NULL")

    op.alter_column("coupon_redemptions", "tenant_id", nullable=False)
    op.execute("ALTER TABLE coupon_redemptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE coupon_redemptions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON coupon_redemptions "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON coupon_redemptions")
    op.execute("ALTER TABLE coupon_redemptions NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE coupon_redemptions DISABLE ROW LEVEL SECURITY")
    op.alter_column("coupon_redemptions", "tenant_id", nullable=True)
