"""Leads ka phone aur coupon ka code bhi PER-DUKAAN — pattern ka aakhri hissa.

t4c1e7f5b2d9 mein rate_card theek hua tha. Us bug ko dhoondhte waqt poore
schema mein wahi shakl khoji gayi (har UNIQUE jo tenant-scoped table par
hai par usme tenant_id nahi) aur do aur nikle:

LEADS. `phone` globally unique tha. Ek aadmi do laundry mein poochh-taachh
kare — jo bilkul aam hai — aur doosri dukaan ka lead INSERT phat jaata.

COUPONS. `code` KHUD primary key tha, yaani poore platform par ek hi
"OFF10". Har dukaan OFF10 / WELCOME / DIWALI chahti hai. Aur iska rasta
isse bhi bura tha: agent_admin pehle `db.get(Coupon, code)` se "already
exists" jaanchta hai, par RLS doosri dukaan ka coupon chhupa deta hai —
to check nikal jaata, INSERT unique violation deta, aur owner ko saaf
"ye code pehle se hai" ki jagah 500 milta.

Coupons/redemptions abhi KHAALI hain, isliye PK badalna aaj sasta hai aur
saal bhar baad mehnga hota. Surrogate UUID id PK, (tenant_id, code) par
unique, aur redemptions ka FK usi jodi par — integrity chhodni nahi padi.
"""

import sqlalchemy as sa
from alembic import op

revision = "u5d2f8a6c3e1"
down_revision = "t4c1e7f5b2d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- leads ----
    op.drop_index("ix_leads_phone", table_name="leads")
    op.create_index("ix_leads_phone", "leads", ["phone"])          # lookup rehne do
    op.create_index("uq_leads_tenant_phone", "leads", ["tenant_id", "phone"], unique=True)

    # ---- coupons ----
    # Bachche ka FK pehle chhodo, warna parent ki PK badli nahi ja sakti.
    op.drop_constraint(
        "fk_coupon_redemptions_coupon_code_coupons", "coupon_redemptions", type_="foreignkey"
    )
    op.drop_constraint("pk_coupons", "coupons", type_="primary")

    op.add_column(
        "coupons",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
    )
    op.create_primary_key("pk_coupons", "coupons", ["id"])
    op.create_unique_constraint("uq_coupons_tenant_code", "coupons", ["tenant_id", "code"])

    # Redemption ab (tenant_id, code) par jud'ta hai. tenant_id wahan pehle
    # se nahi tha — CouponRedemption TenantScoped nahi hai — isliye jodo.
    op.add_column(
        "coupon_redemptions",
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_coupon_redemptions_tenant_id", "coupon_redemptions", ["tenant_id"]
    )
    op.create_foreign_key(
        "fk_coupon_redemptions_coupon", "coupon_redemptions", "coupons",
        ["tenant_id", "coupon_code"], ["tenant_id", "code"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_coupon_redemptions_coupon", "coupon_redemptions", type_="foreignkey")
    op.drop_index("ix_coupon_redemptions_tenant_id", table_name="coupon_redemptions")
    op.drop_column("coupon_redemptions", "tenant_id")

    # Global PK wapas laane se pehle duplicates hataao, warna DDL beech
    # mein phat kar DB ko aadhe haal mein chhod dega.
    op.execute("DELETE FROM coupons a USING coupons b WHERE a.ctid > b.ctid AND a.code = b.code")
    op.drop_constraint("uq_coupons_tenant_code", "coupons", type_="unique")
    op.drop_constraint("pk_coupons", "coupons", type_="primary")
    op.drop_column("coupons", "id")
    op.create_primary_key("pk_coupons", "coupons", ["code"])
    op.create_foreign_key(
        "fk_coupon_redemptions_coupon_code_coupons", "coupon_redemptions", "coupons",
        ["coupon_code"], ["code"],
    )

    op.drop_index("uq_leads_tenant_phone", table_name="leads")
    op.drop_index("ix_leads_phone", table_name="leads")
    op.create_index("ix_leads_phone", "leads", ["phone"], unique=True)
