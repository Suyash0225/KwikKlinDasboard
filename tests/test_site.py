"""Public website (/laundry): rate list CRM se aati hai, aur SIRF home dukaan ki.

Platform par jab doosri dukaanein judengi, har ek ka apna rate card hoga.
Kwik Klin ki website par kabhi kisi aur dukaan ka daam nahi dikhna chahiye —
chahe page kholne wala us dukaan ka logged-in owner hi kyun na ho (middleware
tenant cookie se chunta hai, isliye ye asli khatra hai).
"""

from decimal import Decimal

from sqlalchemy import select, text as sqltext

from app.database import async_session_factory
from app.models import Rate
from app.services import site_rates, tenant_context
from tests.test_data_isolation import _fresh

OTHER_GARMENT = "Zzsitetest Coat"


async def _home_active_rate_count() -> int:
    home = await tenant_context.get_home_tenant_id()
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            return len((await db.execute(select(Rate).where(Rate.is_active))).scalars().all())


async def test_site_shows_only_home_rates_even_in_another_shops_context(client) -> None:
    other = await _fresh("site-rates-other", "rate_card")
    token = tenant_context.current_tenant_id.set(other)
    try:
        async with async_session_factory() as db:
            db.add(Rate(service="Dry Clean", garment=OTHER_GARMENT, unit="pc", rate=Decimal("9999")))
            await db.commit()

        # Doosri dukaan ke context mein website render — jaise uska owner page khole
        site_rates._cache.update(at=0.0, rows=None)
        card = await site_rates.rate_card()
    finally:
        tenant_context.current_tenant_id.reset(token)

    labels = [label for c in card.values() for label, *_ in c["items"]]
    assert OTHER_GARMENT not in labels
    assert len(labels) == await _home_active_rate_count()

    r = await client.get("/")
    assert r.status_code == 200
    assert OTHER_GARMENT not in r.text
    assert "{{" not in r.text  # har placeholder bhara gaya

    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM rate_card WHERE tenant_id = :t"), {"t": other})
        await db.commit()


async def test_rate_change_in_crm_reaches_the_site() -> None:
    home = await tenant_context.get_home_tenant_id()
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            row = Rate(service="Dry Clean", garment="Zzsitetest Jacket", unit="pc", rate=Decimal("321"))
            db.add(row)
            await db.commit()
            rid = row.id
    try:
        site_rates._cache.update(at=0.0, rows=None)
        page = await site_rates.render("{{RATE_PANELS}}")
        assert '<li data-price="₹321">Zzsitetest Jacket</li>' in page
    finally:
        async with tenant_context.as_tenant(home):
            async with async_session_factory() as db:
                await db.execute(sqltext("DELETE FROM rate_card WHERE id = :i"), {"i": rid})
                await db.commit()
        site_rates._cache.update(at=0.0, rows=None)
