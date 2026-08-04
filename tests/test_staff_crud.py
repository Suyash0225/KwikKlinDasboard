"""Staff CRUD from the settings page — server-side rules, not client trust.

The dangerous ones are the delete guards: deleting someone who still owns
active orders would orphan that work, and deleting the default washer would
leave new orders pointing at a phone number that no longer exists.
"""

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
