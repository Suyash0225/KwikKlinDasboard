"""Rate card ab PER-DUKAAN unique — (tenant_id, service, garment).

Wahi galti jo tasks aur customers par o8d5f2c9e6b3 mein theek hui thi, ek
table par reh gayi thi. `uq_service_garment` sirf (service, garment) par
tha, tenant ke bina — yaani poore platform par ek hi "Wash & Iron / Shirt"
ho sakta tha.

Iska matlab kaagaz par chhota lagta hai aur asal mein bada hai: platform
ki DOOSRI dukaan apne rate card mein Shirt daal hi nahi sakti thi. Naye
tenant ka onboarding rate card ke pehle hi row par phat jaata
(`seed_rates` / Settings se pehla rate). Ye tab tak nahi dikha jab tak
ek se zyada nakli dukaan banane ki koshish nahi hui — ek dukaan wale
deployment par kabhi nahi phatta.

Har dukaan ka apna rate card wahi niyam hai jo orders, tasks aur customers
par pehle se hai.
"""

from alembic import op

revision = "t4c1e7f5b2d9"
down_revision = "s3b9d6e4a1c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_service_garment", "rate_card", type_="unique")
    op.create_unique_constraint(
        "uq_rate_tenant_service_garment",
        "rate_card",
        ["tenant_id", "service", "garment"],
    )


def downgrade() -> None:
    # Wapas jaane par do dukaanon ke ek jaise rows constraint todenge.
    # Isliye pehle duplicates hatao (sabse purani row rehne do), warna
    # downgrade beech mein phat kar DB ko aadhe haal mein chhod dega.
    op.execute(
        """
        DELETE FROM rate_card a USING rate_card b
        WHERE a.ctid > b.ctid
          AND a.service = b.service
          AND a.garment = b.garment
        """
    )
    op.drop_constraint("uq_rate_tenant_service_garment", "rate_card", type_="unique")
    op.create_unique_constraint("uq_service_garment", "rate_card", ["service", "garment"])
