"""Staff CRUD from the settings page — server-side rules, not client trust.

The dangerous ones are the delete guards: deleting someone who still owns
active orders would orphan that work, and deleting the default washer would
leave new orders pointing at a phone number that no longer exists.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session_factory
from app.models import Conversation, Order, Staff, StaffRole
from app.services import app_settings, order_service
from tests.conftest import TEST_CUSTOMER_PHONE, purge_phones

H = {"X-API-Key": settings.ADMIN_API_KEY}
CRUD_PHONE_10 = "9999900081"
CRUD_PHONE = "+919999900081"
OTHER_PHONE_10 = "9999900082"
OTHER_PHONE = "+919999900082"


async def _purge_staff(*phones: str) -> None:
    async with async_session_factory() as s:
        rows = (
            await s.execute(select(Staff).where(Staff.phone.in_(phones)))
        ).scalars().all()
        for st in rows:
            await s.execute(delete(Conversation).where(Conversation.staff_id == st.id))
            await s.execute(
                Order.__table__.update()
                .where(Order.assigned_washer_id == st.id)
                .values(assigned_washer_id=None)
            )
            await s.delete(st)
        await s.commit()


@pytest.fixture(autouse=True)
async def _clean_staff():
    await _purge_staff(CRUD_PHONE, OTHER_PHONE)
    yield
    await _purge_staff(CRUD_PHONE, OTHER_PHONE)
    async with async_session_factory() as db:
        for key in ("default_washer_phone", "default_delivery_phone"):
            if await app_settings.get(db, key) in (CRUD_PHONE, OTHER_PHONE):
                await app_settings.set_value(db, key, "")


async def _create(client, phone: str = CRUD_PHONE_10, name: str = "Crudwala") -> str:
    r = await client.post(
        "/admin/api/staff", headers=H,
        json={"name": name, "phone": phone, "role": "WASHER"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- create + list ---

async def test_list_sorts_active_first_then_alphabetical(client) -> None:
    a = await _create(client, CRUD_PHONE_10, "Zeeshan")
    await _create(client, OTHER_PHONE_10, "Aarav")
    await client.put(f"/admin/api/staff/{a}", headers=H, json={"is_active": False})

    rows = (await client.get("/admin/api/staff", headers=H)).json()
    ours = [r for r in rows if r["phone"] in (CRUD_PHONE, OTHER_PHONE)]
    assert [r["name"] for r in ours] == ["Aarav", "Zeeshan"], "active first"
    assert ours[0]["is_active"] and not ours[1]["is_active"]


async def test_duplicate_phone_rejected(client) -> None:
    await _create(client)
    r = await client.post(
        "/admin/api/staff", headers=H,
        json={"name": "Doosra", "phone": CRUD_PHONE_10, "role": "DELIVERY"},
    )
    assert r.status_code == 409


# --- update ---

async def test_update_name_phone_role(client) -> None:
    sid = await _create(client)
    r = await client.put(
        f"/admin/api/staff/{sid}", headers=H,
        json={"name": "Naya Naam", "phone": OTHER_PHONE_10, "role": "DELIVERY"},
    )
    assert r.status_code == 200
    row = next(
        x for x in (await client.get("/admin/api/staff", headers=H)).json()
        if x["id"] == sid
    )
    assert row["name"] == "Naya Naam"
    assert row["phone"] == OTHER_PHONE
    assert row["role"] == "DELIVERY"


async def test_update_to_existing_phone_rejected(client) -> None:
    await _create(client, CRUD_PHONE_10, "Pehla")
    sid2 = await _create(client, OTHER_PHONE_10, "Doosra")
    r = await client.put(f"/admin/api/staff/{sid2}", headers=H, json={"phone": CRUD_PHONE_10})
    assert r.status_code == 409
    assert "already" in r.json()["detail"]


async def test_short_name_rejected_server_side(client) -> None:
    sid = await _create(client)
    r = await client.put(f"/admin/api/staff/{sid}", headers=H, json={"name": "A"})
    assert 400 <= r.status_code < 500, "client-side validation must not be the only guard"


async def test_phone_change_follows_default_washer(client) -> None:
    sid = await _create(client)
    async with async_session_factory() as db:
        await app_settings.set_value(db, "default_washer_phone", CRUD_PHONE)

    await client.put(f"/admin/api/staff/{sid}", headers=H, json={"phone": OTHER_PHONE_10})
    async with async_session_factory() as db:
        assert await app_settings.get(db, "default_washer_phone") == OTHER_PHONE


async def test_number_that_is_also_a_customer_warns_but_never_blocks(client) -> None:
    """Owner jab chahe number badal sake — par chup-chaap nahi.

    Ek hi number staff aur customer dono ho, to uske message STAFF ki tarah
    handle hote hain aur customer wala AI reply band ho jata hai. Pehle ye
    bina kisi ishare ke hota tha; ab server bata deta hai, rokta nahi.
    """
    from app.models import Customer

    sid = await _create(client)
    async with async_session_factory() as db:
        db.add(Customer(phone=OTHER_PHONE, name="Kiran Grahak"))
        await db.commit()
    try:
        r = await client.put(
            f"/admin/api/staff/{sid}", headers=H, json={"phone": OTHER_PHONE_10}
        )
        assert r.status_code == 200, r.text
        warning = r.json().get("warning") or ""
        assert "Kiran Grahak" in warning and OTHER_PHONE in warning
        # aur badlaav sach mein hua — warning ne roka nahi
        async with async_session_factory() as db:
            st = (
                await db.execute(select(Staff).where(Staff.phone == OTHER_PHONE))
            ).scalar_one()
            assert str(st.id) == sid
    finally:
        await purge_phones(OTHER_PHONE)


async def test_staff_list_flags_a_number_that_is_also_a_customer(client) -> None:
    """Ye haalat list mein hamesha dikhni chahiye, sirf save ke waqt nahi."""
    from app.models import Customer

    sid = await _create(client)
    clean = await _create(client, phone=OTHER_PHONE_10, name="Sirf Staff")
    async with async_session_factory() as db:
        db.add(Customer(phone=CRUD_PHONE, name="Dono Jagah"))
        await db.commit()
    try:
        rows = (await client.get("/admin/api/staff", headers=H)).json()
        assert next(r for r in rows if r["id"] == sid)["also_customer"] is True
        assert next(r for r in rows if r["id"] == clean)["also_customer"] is False
    finally:
        await purge_phones(CRUD_PHONE)


async def test_number_change_reopens_the_window_question(client) -> None:
    """24h window NUMBER ki hai, insaan ki nahi.

    Purane number ka window naye par chhod dete to system free-form message
    bhejta, Meta thukra deta, aur wo chupchaap retry queue mein pada rehta —
    staff ko kuch milta hi nahi. Reset ke baad pehla message template se
    jata hai, yaani turant pahunchta hai.
    """
    from datetime import datetime, timezone

    sid = await _create(client)
    async with async_session_factory() as db:
        st = await db.get(Staff, uuid.UUID(sid))
        st.last_message_at = datetime.now(timezone.utc)
        await db.commit()

    r = await client.put(
        f"/admin/api/staff/{sid}", headers=H, json={"phone": OTHER_PHONE_10}
    )
    assert r.status_code == 200
    async with async_session_factory() as db:
        st = await db.get(Staff, uuid.UUID(sid))
        assert st.phone == OTHER_PHONE
        assert st.last_message_at is None, "naye number ki window khuli nahi maani jani chahiye"
    # aur owner ko saaf bata bhi diya jaye ki ab kya hoga
    assert "template" in (r.json().get("ready") or "")


async def test_name_change_keeps_the_open_window(client) -> None:
    """Sirf naam badla — number wahi hai, to chat band nahi honi chahiye."""
    from datetime import datetime, timezone

    sid = await _create(client)
    async with async_session_factory() as db:
        st = await db.get(Staff, uuid.UUID(sid))
        st.last_message_at = datetime.now(timezone.utc)
        await db.commit()

    r = await client.put(f"/admin/api/staff/{sid}", headers=H, json={"name": "Naya Naam"})
    assert r.status_code == 200
    async with async_session_factory() as db:
        st = await db.get(Staff, uuid.UUID(sid))
        assert st.last_message_at is not None
        assert st.name == "Naya Naam"
    assert "khuli" in (r.json().get("ready") or "")


async def test_plain_number_change_carries_no_warning(client) -> None:
    sid = await _create(client)
    r = await client.put(
        f"/admin/api/staff/{sid}", headers=H, json={"phone": OTHER_PHONE_10}
    )
    assert r.status_code == 200 and r.json().get("warning") is None


# --- deactivate / reactivate ---

async def test_deactivate_then_reactivate(client) -> None:
    sid = await _create(client)
    assert (await client.delete(f"/admin/api/staff/{sid}", headers=H)).status_code == 200
    row = next(x for x in (await client.get("/admin/api/staff", headers=H)).json() if x["id"] == sid)
    assert row["is_active"] is False

    assert (
        await client.put(f"/admin/api/staff/{sid}", headers=H, json={"is_active": True})
    ).status_code == 200
    row = next(x for x in (await client.get("/admin/api/staff", headers=H)).json() if x["id"] == sid)
    assert row["is_active"] is True


# --- hard delete guards ---

async def test_hard_delete_blocked_by_active_orders(client, sent) -> None:
    sid = await _create(client)
    try:
        async with async_session_factory() as db:
            order = await order_service.create_order(
                db, customer_phone=TEST_CUSTOMER_PHONE, customer_name="Assign Test",
                items=[{"type": "shirt", "qty": 1, "service": "wash"}],
                total_amount=Decimal("100"), created_by="test",
            )
            staff = await db.get(Staff, __import__("uuid").UUID(sid))
            order.assigned_washer_id = staff.id
            await db.commit()

        rows = (await client.get("/admin/api/staff", headers=H)).json()
        row = next(x for x in rows if x["id"] == sid)
        assert row["active_orders"] == 1, "UI needs the count to explain the block"

        r = await client.delete(f"/admin/api/staff/{sid}/permanent", headers=H)
        assert r.status_code == 409
        assert "1 active order" in r.json()["detail"]

        # deactivate must still be possible
        assert (await client.delete(f"/admin/api/staff/{sid}", headers=H)).status_code == 200
    finally:
        await purge_phones(TEST_CUSTOMER_PHONE)


async def test_hard_delete_clears_default_washer(client) -> None:
    sid = await _create(client)
    async with async_session_factory() as db:
        await app_settings.set_value(db, "default_washer_phone", CRUD_PHONE)

    r = await client.delete(f"/admin/api/staff/{sid}/permanent", headers=H)
    assert r.status_code == 200

    async with async_session_factory() as db:
        assert await app_settings.get(db, "default_washer_phone") == ""
    rows = (await client.get("/admin/api/staff", headers=H)).json()
    assert not any(x["id"] == sid for x in rows), "row must be gone"


async def test_hard_delete_removes_chat_history(client) -> None:
    sid = await _create(client)
    async with async_session_factory() as db:
        import uuid as _u

        from app.models import Direction

        db.add(
            Conversation(
                staff_id=_u.UUID(sid), direction=Direction.OUTBOUND,
                message_text="kaam hai", wa_message_id="wamid.TESTcrud1", sent_by="bot",
            )
        )
        await db.commit()

    assert (await client.delete(f"/admin/api/staff/{sid}/permanent", headers=H)).status_code == 200
    async with async_session_factory() as db:
        left = (
            await db.execute(
                select(Conversation).where(Conversation.wa_message_id == "wamid.TESTcrud1")
            )
        ).scalars().all()
    assert left == [], "conversations need a participant, so they go with the staff row"


async def test_delete_unknown_staff_is_404(client) -> None:
    import uuid as _u

    r = await client.delete(f"/admin/api/staff/{_u.uuid4()}/permanent", headers=H)
    assert r.status_code == 404


async def test_delete_lets_go_of_old_tasks(client, sent) -> None:
    """Asli shikayat (09 Aug): staff delete hi nahi ho raha tha.

    Wajah: uske naam par purane TASKS the. Delete order aur conversation
    to chhod deta tha, tasks nahi — FK tootti thi aur owner ko sirf
    "purana record juda hua hai, deactivate kar dijiye" dikhta tha.
    Tasks business ka record hain: aadmi jaata hai, kaam ka itihaas rehta
    hai — bas uska naam hat jata hai.
    """
    from app.services import tasks as task_service

    sid = await _create(client, CRUD_PHONE_10, "Purana Ladka")
    async with async_session_factory() as db:
        st = await db.get(Staff, uuid.UUID(sid))
        t = await task_service.create_task(
            db, title="uska purana kaam", staff=st, notify=False
        )
        code = t.code

    r = await client.delete(f"/admin/api/staff/{sid}/permanent", headers=H)
    assert r.status_code == 200, r.text
    async with async_session_factory() as db:
        gone = (
            await db.execute(select(Staff).where(Staff.id == uuid.UUID(sid)))
        ).scalar_one_or_none()
        assert gone is None, "staff delete hona chahiye"
        left = await task_service.get_by_code(db, code)
        assert left is not None and left.assigned_staff_id is None, \
            "kaam ka record rehna chahiye, bas bina naam ke"
        from sqlalchemy import text as _sql

        await db.execute(_sql("DELETE FROM tasks WHERE code = :c"), {"c": code})
        await db.commit()


async def test_a_senior_washerman_can_be_a_manager_too(client) -> None:
    """Chhoti dukaan ka sach: sabse senior washerman hi sab sambhalta hai.

    Use sirf "Washerman" kehna uske kaam ko chhota dikhata hai, aur poora
    "Manager" bana dene par uska apna dhulai ka kaam list se gayab ho
    jata. SUPERVISOR dono hai.
    """
    r = await client.post(
        "/admin/api/staff", headers=H,
        json={"name": "Senior Bhai", "phone": OTHER_PHONE_10, "role": "SUPERVISOR"},
    )
    assert r.status_code == 201, r.text
    rows = (await client.get("/admin/api/staff", headers=H)).json()
    mine = next(x for x in rows if x["id"] == r.json()["id"])
    assert mine["role"] == "SUPERVISOR"

    # aur uske paas manager wali taakat hoti hai
    from app.models import StaffRole
    from app.services.staff_auth import MANAGER_ROLES

    assert StaffRole.SUPERVISOR in MANAGER_ROLES
