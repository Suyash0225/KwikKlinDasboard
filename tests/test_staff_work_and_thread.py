"""Staff panel ke naye hisse: apna kaam dikhna, sawaal-jawab, notifications.

Teen shikayatein jo ye file pakadti hai:
1. Bill kisi ne bhi banaya ho — pickup/delivery us aadmi ko dikhna chahiye
   jo dukaan mein wo kaam karta hai. Pehle sirf explicitly assigned order
   dikhte the, aur zyadatar bill kisi ke naam par bante hi nahi.
2. Staff ne "Ask" kiya to sawaal kahin store hi nahi hota tha, aur jawab
   dene ka koi rasta nahi tha.
3. Jawab aaya ye staff ko pata kaise chale — badge.
"""

import pytest
from sqlalchemy import select, text as sqltext

from app.database import async_session_factory
from app.models import Order, OrderStatus, Staff, StaffRole, Task
from app.services import app_settings, tenant_context
from tests.conftest import purge_phones
from tests.test_staff_panel import (
    A_MGR_PHONE,
    A_PHONE,
    CUST_A,
    _login,
    _order_for,
    _task_for,
    two_shops,  # noqa: F401  (fixture re-export)
)

DEL_PHONE = "+919999900601"
DEL_CUST = "+919999900602"


@pytest.fixture
async def delivery_guy(two_shops):  # noqa: F811
    """Dukaan A ka delivery wala, panel login ke saath."""
    from app.services import staff_auth

    tok = tenant_context.current_tenant_id.set(two_shops["a"])
    try:
        async with async_session_factory() as db:
            st = Staff(phone=DEL_PHONE, name="Deliverywala", role=StaffRole.DELIVERY)
            db.add(st)
            await db.commit()
            await db.refresh(st)
            from tests.test_staff_panel import PW

            await staff_auth.set_password(db, st, PW, temp=False)
            await app_settings.set_value(db, "default_delivery_phone", DEL_PHONE)
            await db.commit()
            sid = st.id
    finally:
        tenant_context.current_tenant_id.reset(tok)
    try:
        yield sid
    finally:
        tenant_context.current_tenant_id.set(None)
        await purge_phones(DEL_CUST)
        async with async_session_factory() as db:
            # settings_kv ki row poori hatani hai, khali karna kaafi nahi —
            # warna two_shops ka teardown tenant delete par FK pe girta hai.
            await db.execute(
                sqltext(
                    "DELETE FROM settings_kv WHERE key = 'default_delivery_phone'"
                    " AND tenant_id = :t"
                ),
                {"t": two_shops["a"]},
            )
            await db.execute(
                sqltext("DELETE FROM staff_sessions WHERE staff_id = :i"), {"i": sid}
            )
            await db.execute(sqltext("DELETE FROM staff WHERE phone = :p"), {"p": DEL_PHONE})
            await db.commit()


async def test_unassigned_pickup_still_reaches_the_delivery_boy(
    client, two_shops, delivery_guy, sent  # noqa: F811
) -> None:
    """Manager ne bill banaya, kisi ko assign nahi kiya. Work order to
    dukaan ke delivery wale ko hi jaata hai — panel bhi wahi dikhaye."""
    tok = tenant_context.current_tenant_id.set(two_shops["a"])
    try:
        async with async_session_factory() as db:
            from app.models import Customer

            c = Customer(phone=DEL_CUST, name="Bina Assign")
            db.add(c)
            await db.flush()
            o = Order(
                order_number="KK-UNASSIGNED-01", customer_id=c.id,
                status=OrderStatus.PICKUP_ASSIGNED, items=[{"type": "Shirt", "qty": 2}],
                total_amount=100,
            )
            db.add(o)
            await db.commit()
    finally:
        tenant_context.current_tenant_id.reset(tok)

    await _login(client, DEL_PHONE)
    r = await client.get("/staff/api/route")
    assert r.status_code == 200, r.text
    numbers = [s["number"] for s in r.json()["stops"]]
    assert "KK-UNASSIGNED-01" in numbers, (
        "bina assign kiya order dukaan ke delivery wale ko dikhna chahiye"
    )


async def test_ask_is_saved_and_the_owner_can_answer(client, test_washer, sent) -> None:
    """Sawaal thread mein, jawab thread mein, aur staff ko badge.

    HOME dukaan par: owner ka dashboard (ADMIN_API_KEY) home context mein
    chalta hai, isliye staff bhi home ka hona chahiye — warna RLS use task
    dikhati hi nahi, jo bilkul sahi hai.
    """
    from app.config import settings

    from app.services import staff_auth, tasks as task_service
    from tests.conftest import TEST_CUSTOMER_PHONE, TEST_WASHER_PHONE
    from tests.test_staff_panel import PW

    home = await tenant_context.get_home_tenant_id()
    async with async_session_factory() as db:
        st = await db.get(Staff, test_washer)
        await staff_auth.set_password(db, st, PW, temp=False)
        t = await task_service.create_task(
            db, title="Kurta dhona hai", staff=st, order=None, notify=False
        )
        code = t.code

    await _login(client, TEST_WASHER_PHONE)
    # 1. staff poochta hai
    r = await client.post(f"/staff/api/tasks/{code}/ask", json={"text": "Address sahi hai?"})
    assert r.status_code == 200, r.text

    # abhi koi jawab nahi -> badge khali
    assert (await client.get("/staff/api/notifications")).json()["unread"] == 0

    # 2. owner dashboard se jawab deta hai
    admin = {"X-API-Key": settings.ADMIN_API_KEY}
    client.cookies.clear()
    r = await client.post(
        f"/admin/api/tasks/{code}/reply", headers=admin,
        json={"text": "Haan, wahi address — gate ke paas"},
    )
    assert r.status_code == 200, r.text

    # 3. staff ko badge dikhta hai
    await _login(client, TEST_WASHER_PHONE)
    n = (await client.get("/staff/api/notifications")).json()
    assert n["unread"] == 1
    assert n["items"][0]["code"] == code

    # 4. thread kholte hi dono baatein, aur badge saaf
    th = (await client.get(f"/staff/api/tasks/{code}/messages")).json()
    assert [m["who"] for m in th["messages"]] == ["staff", "owner"]
    assert "gate ke paas" in th["messages"][1]["text"]
    assert (await client.get("/staff/api/notifications")).json()["unread"] == 0


async def test_bills_can_be_searched_and_filtered(client, two_shops, sent) -> None:  # noqa: F811
    """Counter par bill dhoondhna: number se, naam se, ya sirf udhaar wale."""
    from decimal import Decimal

    from app.models import Rate

    tok = tenant_context.current_tenant_id.set(two_shops["a"])
    try:
        async with async_session_factory() as db:
            db.add(Rate(service="FiltSvc", garment="Coat", unit="pc", rate=Decimal("90")))
            await db.commit()
    finally:
        tenant_context.current_tenant_id.reset(tok)

    await _login(client, A_MGR_PHONE)
    try:
        r = await client.post("/staff/api/bills", json={
            "customer_phone": CUST_A, "customer_name": "Filter Wali",
            "items": [{"service": "FiltSvc", "garment": "Coat", "qty": 1}],
        })
        assert r.status_code == 201, r.text
        num = r.json()["order_number"]

        # naam se
        got = [b["number"] for b in (await client.get("/staff/api/bills?q=Filter")).json()]
        assert num in got
        # number se
        got = [b["number"] for b in (await client.get(f"/staff/api/bills?q={num}")).json()]
        assert num in got
        # na milne wala kuch
        assert (await client.get("/staff/api/bills?q=zzzznotathing")).json() == []
        # udhaar wale (abhi kuch nahi diya, to due list mein hona chahiye)
        got = [b["number"] for b in (await client.get("/staff/api/bills?pay=due")).json()]
        assert num in got
        assert num not in [b["number"] for b in (await client.get("/staff/api/bills?pay=paid")).json()]
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM rate_card WHERE service = 'FiltSvc'"))
            await db.commit()
