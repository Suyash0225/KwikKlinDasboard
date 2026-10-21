"""Dashboard se Washing task — bina grahak ke bhi, aur order par dobara dene
par purana khula kaam owner ki baat se update ho (chupchaap nahi)."""

from sqlalchemy import delete

from app.config import settings
from app.database import async_session_factory
from app.models import Task
from app.services import order_service

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


async def test_shop_wash_task_needs_no_customer(client) -> None:
    r = await client.post("/admin/api/tasks", json={
        "title": "Saare towel dho do", "kind": "wash",
    }, headers=AUTH)
    assert r.status_code == 201, r.text
    code = r.json()["code"]
    async with async_session_factory() as db:
        await db.execute(delete(Task).where(Task.code == code))
        await db.commit()


async def test_second_wash_task_on_an_order_updates_the_open_one(client) -> None:
    async with async_session_factory() as db:
        order = await order_service.create_order(
            db, customer_phone="+919999900377", customer_name="Wash Task",
            items=[{"type": "Shirt", "qty": 2}], created_by="test",
        )
        number = order.order_number
    first = await client.post("/admin/api/tasks", json={
        "title": "Pehla instruction", "kind": "wash", "order_number": number,
    }, headers=AUTH)
    assert first.status_code == 201, first.text
    second = await client.post("/admin/api/tasks", json={
        "title": "Collar par daag hai, dhyan se", "kind": "wash", "order_number": number,
    }, headers=AUTH)
    assert second.status_code == 201, second.text
    body = second.json()
    assert body["updated_existing"] is True and body["code"] == first.json()["code"]
    async with async_session_factory() as db:
        from sqlalchemy import select

        t = (await db.execute(select(Task).where(Task.code == body["code"]))).scalar_one()
        assert t.title == "Collar par daag hai, dhyan se"


async def test_pickup_task_still_needs_a_customer(client) -> None:
    r = await client.post("/admin/api/tasks", json={"title": "Kapde lao", "kind": "pickup"}, headers=AUTH)
    assert r.status_code == 400
