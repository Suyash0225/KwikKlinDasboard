"""Data isolation proof: Tenant A (home / Kwik Klin) aur Tenant B ek doosre
ka data NAHI dekh sakte — teeno layers par test:

1. ORM layer   — automatic tenant filter (do_orm_execute)
2. DB layer    — Postgres RLS, raw SQL se bhi paar nahi hota (read + write)
3. HTTP layer  — doosre tenant ka logged-in user /admin API se 403 khata hai,
                 aur home tenant ke API responses mein B ka data nahi aata

System context (ContextVar unset) scheduler/webhook ke liye full access
rakhta hai — uska bhi ek test hai, kyunki wahi backup/replay ki guarantee hai.
"""

import uuid

import pytest
from sqlalchemy import select, text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models import Customer
from app.models.tenant import Tenant, User
from app.services import auth, tenant_context

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}

# Recognizable markers, same convention as the rest of the suite.
HOME_PHONE = "+919999900081"
B_PHONE = "+919999900082"
B_SLUG = "test-isolation-b"
B_EMAIL = "isolation-b@test.local"


async def _home_id() -> uuid.UUID:
    tid = await tenant_context.get_home_tenant_id()
    assert tid is not None, "home tenant must exist (bootstrap_home_tenant)"
    return tid


@pytest.fixture
async def two_tenants():
    """Tenant B + ek customer har tenant mein. System context mein banaya,
    system context mein saaf kiya."""
    home_id = await _home_id()
    async with async_session_factory() as db:
        tenant_b = Tenant(
            slug=B_SLUG,
            shop_name="Isolation Test Laundry",
            owner_name="Tenant B",
            owner_phone="+919999900083",
            plan="starter",
            status="active",
        )
        db.add(tenant_b)
        await db.flush()
        b_id = tenant_b.id
        db.add(Customer(phone=HOME_PHONE, name="Home Grahak", tenant_id=home_id))
        db.add(Customer(phone=B_PHONE, name="B Grahak", tenant_id=b_id))
        await db.commit()
    try:
        yield home_id, b_id
    finally:
        tenant_context.current_tenant_id.set(None)  # cleanup always system ctx
        async with async_session_factory() as db:
            await db.execute(
                sqltext("DELETE FROM customers WHERE phone IN (:p1, :p2)"),
                {"p1": HOME_PHONE, "p2": B_PHONE},
            )
            await db.execute(
                sqltext(
                    "DELETE FROM login_sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE tenant_id = "
                    " (SELECT id FROM tenants WHERE slug = :s))"
                ),
                {"s": B_SLUG},
            )
            await db.execute(
                sqltext(
                    "DELETE FROM users WHERE tenant_id = "
                    "(SELECT id FROM tenants WHERE slug = :s)"
                ),
                {"s": B_SLUG},
            )
            await db.execute(
                sqltext("DELETE FROM tenants WHERE slug = :s"), {"s": B_SLUG}
            )
            await db.commit()


async def test_orm_layer_scopes_selects_per_tenant(two_tenants) -> None:
    """Layer 1: ORM SELECT automatically apne tenant tak simat jaata hai."""
    home_id, b_id = two_tenants

    token = tenant_context.current_tenant_id.set(b_id)
    try:
        async with async_session_factory() as db:
            phones = {
                c.phone
                for c in (await db.execute(select(Customer))).scalars().all()
            }
        assert B_PHONE in phones
        assert HOME_PHONE not in phones, "Tenant B saw home tenant's customer!"
    finally:
        tenant_context.current_tenant_id.reset(token)

    token = tenant_context.current_tenant_id.set(home_id)
    try:
        async with async_session_factory() as db:
            phones = {
                c.phone
                for c in (await db.execute(select(Customer))).scalars().all()
            }
        assert HOME_PHONE in phones
        assert B_PHONE not in phones, "Home tenant saw Tenant B's customer!"
    finally:
        tenant_context.current_tenant_id.reset(token)


async def test_rls_blocks_raw_sql_reads(two_tenants) -> None:
    """Layer 2 (READ): raw SQL — jisme ORM ka filter lagta hi nahi —
    phir bhi Postgres RLS doosre tenant ke rows chhupa deta hai."""
    home_id, b_id = two_tenants

    token = tenant_context.current_tenant_id.set(b_id)
    try:
        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    sqltext("SELECT phone FROM customers WHERE phone IN (:p1, :p2)"),
                    {"p1": HOME_PHONE, "p2": B_PHONE},
                )
            ).scalars().all()
        assert rows == [B_PHONE], f"RLS leak: raw SQL saw {rows}"
    finally:
        tenant_context.current_tenant_id.reset(token)


async def test_rls_blocks_raw_sql_writes(two_tenants) -> None:
    """Layer 2 (WRITE): Tenant B home tenant ka row UPDATE/DELETE karne ki
    koshish kare to Postgres use row dikhata hi nahi — 0 rows affected."""
    home_id, b_id = two_tenants

    token = tenant_context.current_tenant_id.set(b_id)
    try:
        async with async_session_factory() as db:
            r = await db.execute(
                sqltext("UPDATE customers SET name = 'hacked' WHERE phone = :p"),
                {"p": HOME_PHONE},
            )
            await db.commit()
        assert r.rowcount == 0, "Tenant B UPDATED a home-tenant row!"

        async with async_session_factory() as db:
            r = await db.execute(
                sqltext("DELETE FROM customers WHERE phone = :p"),
                {"p": HOME_PHONE},
            )
            await db.commit()
        assert r.rowcount == 0, "Tenant B DELETED a home-tenant row!"
    finally:
        tenant_context.current_tenant_id.reset(token)

    # System context se verify: row untouched hai.
    async with async_session_factory() as db:
        name = (
            await db.execute(
                sqltext("SELECT name FROM customers WHERE phone = :p"),
                {"p": HOME_PHONE},
            )
        ).scalar_one()
    assert name == "Home Grahak"


async def test_rls_with_check_blocks_wrong_tenant_insert(two_tenants) -> None:
    """Layer 2 (INSERT): Tenant B ke context mein home tenant ke naam par row
    ghusane ki koshish — RLS WITH CHECK usi waqt commit rok deta hai."""
    home_id, b_id = two_tenants

    token = tenant_context.current_tenant_id.set(b_id)
    try:
        async with async_session_factory() as db:
            with pytest.raises(Exception) as exc_info:
                await db.execute(
                    sqltext(
                        "INSERT INTO customers (id, phone, name, tenant_id) "
                        "VALUES (:id, '+919999900084', 'smuggled', :tid)"
                    ),
                    {"id": str(uuid.uuid4()), "tid": str(home_id)},
                )
            assert "row-level security" in str(exc_info.value).lower()
            await db.rollback()
    finally:
        tenant_context.current_tenant_id.reset(token)


async def test_api_scopes_other_tenants_session_to_own_data(client, two_tenants) -> None:
    """Layer 3 (self-serve gate ke baad): Tenant B ka logged-in user dashboard
    APIs use KAR SAKTA hai — lekin use milta hai SIRF APNA data. Home ka ek
    byte bhi nahi (RLS + ctx middleware ka proof, poora HTTP raasta)."""
    home_id, b_id = two_tenants
    async with async_session_factory() as db:
        user_b = User(
            tenant_id=b_id,
            name="B Owner",
            email=B_EMAIL,
            password_hash=auth.hash_password("isolation-test-pw"),
            role="OWNER",
        )
        db.add(user_b)
        await db.commit()
        raw_token = await auth.start_session(db, user_b, ip="127.0.0.1", user_agent="pytest")
        await db.commit()

    client.cookies.set("kk_session", raw_token)
    try:
        r = await client.get("/admin/api/customers?limit=500")
        assert r.status_code == 200, r.text[:120]
        assert B_PHONE in r.text, "B ko apna hi customer nahi dikha!"
        assert HOME_PHONE not in r.text, "HOME KA DATA B KE DASHBOARD MEIN LEAK!"
        r = await client.get("/orders")
        assert r.status_code == 200
        assert HOME_PHONE not in r.text
    finally:
        client.cookies.delete("kk_session")


async def test_api_responses_never_contain_other_tenant_data(
    client, two_tenants
) -> None:
    """Layer 3: home dashboard (API key) ke customer list mein Tenant B ka
    customer kabhi nahi aata — poora HTTP->middleware->ORM->RLS raasta."""
    r = await client.get("/admin/api/customers?limit=500", headers=AUTH)
    assert r.status_code == 200
    body = r.text
    assert HOME_PHONE in body, "home tenant's own customer missing?"
    assert B_PHONE not in body, "Tenant B's customer leaked into home API!"


async def test_system_context_still_sees_everything(two_tenants) -> None:
    """Scheduler/backup ki guarantee: system context (koi tenant set nahi)
    mein dono tenants ke rows dikhte hain — warna replay/backup toot jaata."""
    assert tenant_context.current_tenant_id.get() is None
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                sqltext("SELECT phone FROM customers WHERE phone IN (:p1, :p2)"),
                {"p1": HOME_PHONE, "p2": B_PHONE},
            )
        ).scalars().all()
    assert set(rows) == {HOME_PHONE, B_PHONE}


# ---------------------------------------- per-dukaan unique constraints ----


async def _fresh(slug: str, table: str):
    """Tenant do, aur uski purani rows hatao.

    Ye test har baar wahi row daalte hain. Bina safai ke doosra run us
    dukaan ke ANDAR duplicate banata hai aur constraint sahi hi mana kar
    deta — test lal ho jaata bina kisi bug ke.
    """
    tid = await _tenant(slug)
    async with async_session_factory() as db:
        await db.execute(sqltext(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await db.commit()
    return tid


async def _tenant(slug: str):
    """Ek nanga tenant — sirf constraint jaanchne ke liye."""
    from datetime import datetime, timedelta, timezone

    from app.models import TENANT_ACTIVE

    async with async_session_factory() as db:
        t = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
        if t is None:
            t = Tenant(
                slug=slug, shop_name=slug, owner_name="O", owner_phone=f"+9199{abs(hash(slug)) % 10**8:08d}",
                owner_email=f"{slug}@t.test", city="X", plan="pro", status=TENANT_ACTIVE,
                current_period_end=datetime.now(timezone.utc) + timedelta(days=90),
            )
            db.add(t)
            await db.commit()
        return t.id


async def test_two_shops_can_price_the_same_garment(client) -> None:
    """Do dukaanein ek hi service+garment rakh saken.

    rate_card par UNIQUE (service, garment) tha — tenant ke bina. Yaani
    platform ki DOOSRI dukaan apne rate card mein "Shirt" daal hi nahi
    sakti thi, aur uska onboarding pehli hi row par phat jaata. Ek dukaan
    wale deployment par ye kabhi nahi dikhta.
    """
    from decimal import Decimal

    from app.models import Rate

    a, b = await _fresh("iso-rate-a", "rate_card"), await _fresh("iso-rate-b", "rate_card")
    for tid, price in ((a, "45"), (b, "60")):
        token = tenant_context.current_tenant_id.set(tid)
        try:
            async with async_session_factory() as db:
                db.add(Rate(service="Wash & Iron", garment="Shirt", unit="pc",
                            rate=Decimal(price)))
                await db.commit()
        finally:
            tenant_context.current_tenant_id.reset(token)

    # Aur dono ka apna daam — ek doosre ko overwrite nahi kiya
    for tid, want in ((a, 45), (b, 60)):
        token = tenant_context.current_tenant_id.set(tid)
        try:
            async with async_session_factory() as db:
                rows = (await db.execute(select(Rate).where(Rate.garment == "Shirt"))).scalars().all()
                assert len(rows) == 1 and float(rows[0].rate) == want
        finally:
            tenant_context.current_tenant_id.reset(token)


async def test_two_shops_can_use_the_same_coupon_code(client) -> None:
    """"OFF10" har dukaan ka apna ho.

    `code` KHUD primary key thi, yaani poore platform par ek hi OFF10.
    Aur rasta isse bhi bura tha: owner ko "ye code pehle se hai" ki jagah
    500 milta, kyunki duplicate check RLS ke peeche chhupe coupon ko dekh
    hi nahi pata tha.
    """
    from decimal import Decimal

    from app.models import Coupon

    a, b = await _fresh("iso-cpn-a", "coupons"), await _fresh("iso-cpn-b", "coupons")
    for tid, val in ((a, "10"), (b, "25")):
        token = tenant_context.current_tenant_id.set(tid)
        try:
            async with async_session_factory() as db:
                db.add(Coupon(code="OFF10", discount_type="percent", value=Decimal(val)))
                await db.commit()
        finally:
            tenant_context.current_tenant_id.reset(token)

    for tid, want in ((a, 10), (b, 25)):
        token = tenant_context.current_tenant_id.set(tid)
        try:
            async with async_session_factory() as db:
                from app.services.marketing import get_coupon

                c = await get_coupon(db, "off10")     # lookup case-insensitive
                assert c is not None and float(c.value) == want
        finally:
            tenant_context.current_tenant_id.reset(token)


async def test_two_shops_can_have_the_same_lead_phone(client) -> None:
    """Ek aadmi do laundry mein poochh-taachh kar sakta hai."""
    from app.models import Lead

    a, b = await _fresh("iso-lead-a", "leads"), await _fresh("iso-lead-b", "leads")
    for tid, name in ((a, "Poocha A se"), (b, "Poocha B se")):
        token = tenant_context.current_tenant_id.set(tid)
        try:
            async with async_session_factory() as db:
                db.add(Lead(phone="+919812345678", name=name))
                await db.commit()
        finally:
            tenant_context.current_tenant_id.reset(token)

    token = tenant_context.current_tenant_id.set(a)
    try:
        async with async_session_factory() as db:
            rows = (await db.execute(select(Lead).where(Lead.phone == "+919812345678"))).scalars().all()
            assert len(rows) == 1 and rows[0].name == "Poocha A se"
    finally:
        tenant_context.current_tenant_id.reset(token)


async def test_rls_is_actually_in_force_not_just_configured(client) -> None:
    """Schema nahi — ASAL vyavhaar jaancho.

    Har tenant table par ENABLE + FORCE ROW LEVEL SECURITY laga hai aur
    schema dekh kar sab theek lagta hai. Par SUPERUSER RLS ko poori tarah
    nazarandaz karta hai, aur FORCE uspar laagu nahi hota — FORCE sirf
    TABLE OWNER ke liye hai. docker-compose ka POSTGRES_USER Postgres ka
    bootstrap superuser hai, isliye default setup mein defence-in-depth ki
    teesri parat maujood hi nahi hoti.

    Ye test us haalat mein SKIP hota hai (fail nahi) — kyunki wo deployment
    ki kami hai, code ki nahi, aur suite ko laal rakhne se sirf log lal
    rehne ke aadi ho jaate hain. Jis din app NOSUPERUSER role par jayegi,
    ye test apne aap pehra dena shuru kar dega.
    """
    async with async_session_factory() as db:
        bypasses = (
            await db.execute(
                sqltext("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).scalar_one()
    if bypasses:
        pytest.skip(
            "DB role bypasses RLS (superuser/BYPASSRLS) — isolation rests on "
            "the ORM filter alone. Use a NOSUPERUSER app role to enable this check."
        )

    a, b = await _fresh("rls-live-a", "customers"), await _fresh("rls-live-b", "customers")
    async with async_session_factory() as db:
        for tid, name in ((a, "A ka grahak"), (b, "B ka grahak")):
            await db.execute(
                sqltext("INSERT INTO customers (id, tenant_id, phone, name) "
                        "VALUES (gen_random_uuid(), :t, :p, :n)"),
                {"t": tid, "p": f"+9190{str(tid)[:8]}", "n": name},
            )
        await db.commit()

    # RAW SQL — jaan-boojh kar. ORM ka filter yahan lagta hi nahi, isliye
    # jo bhi rokta hai wo sirf RLS hai. Yahi is test ka poora maqsad hai.
    async with async_session_factory() as db:
        await db.execute(sqltext(f"SET LOCAL app.tenant_id = '{a}'"))
        rows = (
            await db.execute(sqltext("SELECT name FROM customers WHERE phone LIKE '+9190%'"))
        ).scalars().all()
    assert rows == ["A ka grahak"], f"RLS ne doosri dukaan ki row nahi roki: {rows}"
