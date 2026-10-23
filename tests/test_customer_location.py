"""📍 Grahak ka Google Maps pin — staff darwaze par save kare, owner link
daale, aur agla ladka Navigate dabaye (staff_panel + admin)."""

import uuid

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import Customer, Staff
from app.services import app_settings, tenant_context
from app.services.order_service import create_order
from tests.conftest import TEST_CUSTOMER_PHONE, purge_phones
from tests.test_staff_panel import A_DEL_PHONE, A_PHONE, PW, _login, two_shops  # noqa: F401

H = {"X-API-Key": settings.ADMIN_API_KEY}
CUST = "+919999900207"
LAT, LNG = 25.3176, 82.9739


async def _order_for(tenant_id, staff_id, phone: str):
    """Delivery boy ka ek pickup order + task — (order_number, customer_ref)."""
    from app.services import tasks as task_service

    async with tenant_context.as_tenant(tenant_id):
        async with async_session_factory() as db:
            order = await create_order(
                db, customer_phone=phone, customer_name="Pin Grahak",
                items=[{"type": "Kurta", "qty": 1}], created_by="test", needs_pickup=True,
            )
            st = await db.get(Staff, staff_id)
            await task_service.create_task(db, title="Pickup", staff=st, order=order, notify=False, kind="pickup")
            return order.order_number, str(order.customer_id)


async def _show_route(tenant_id, on: bool) -> None:
    async with tenant_context.as_tenant(tenant_id):
        async with async_session_factory() as db:
            await app_settings.set_value(db, "staff_show_route", on)


async def test_delivery_boy_saves_pin_and_everyone_can_navigate(client, two_shops, sent) -> None:
    number, ref = await _order_for(two_shops["a"], two_shops["a_del"], CUST)
    await _show_route(two_shops["a"], True)
    await _login(client, A_DEL_PHONE)

    # pin se pehle: stop par Navigate nahi, bill-context jaisa "no location"
    stop = next(s for s in (await client.get("/staff/api/route")).json()["stops"] if s["number"] == number)
    assert stop["has_location"] is False and stop["nav_url"] == "" and stop["customer_ref"] == ref

    r = await client.post(f"/staff/api/customers/{ref}/location", json={"lat": LAT, "lng": LNG, "accuracy_m": 12})
    assert r.status_code == 200, r.text
    assert r.json()["has_location"] and "destination=25.317600,82.973900" in r.json()["nav_url"]

    stop = next(s for s in (await client.get("/staff/api/route")).json()["stops"] if s["number"] == number)
    assert stop["has_location"] and stop["waypoint"] == "25.317600,82.973900"
    card = next(t for t in (await client.get("/staff/api/tasks?tab=mine")).json()["tasks"] if t["order"]["number"] == number)
    assert card["order"]["nav_url"].startswith("https://www.google.com/maps/dir/")
    detail = (await client.get(f"/staff/api/orders/{number}")).json()
    assert detail["nav_url"] == card["order"]["nav_url"] and detail["customer_ref"] == ref

    # naya ladka: naam likho, Navigate milega
    hits = (await client.get("/staff/api/customers/locate?q=Pin Gra")).json()
    assert [h["ref"] for h in hits] == [ref] and hits[0]["has_location"]
    assert CUST not in str(hits), "poora number kabhi nahi"

    async with tenant_context.as_tenant(two_shops["a"]):
        async with async_session_factory() as db:
            cust = (await db.execute(select(Customer).where(Customer.phone == CUST))).scalar_one()
            assert cust.location_by == "Adel" and cust.location_at is not None


async def test_bad_gps_and_wrong_roles_are_refused(client, two_shops, sent) -> None:
    number, ref = await _order_for(two_shops["a"], two_shops["a_del"], CUST)
    await _show_route(two_shops["a"], True)
    await _login(client, A_DEL_PHONE)
    r = await client.post(f"/staff/api/customers/{ref}/location", json={"lat": LAT, "lng": LNG, "accuracy_m": 900})
    assert r.status_code == 400 and "accurate" in r.json()["detail"]
    r = await client.post(f"/staff/api/customers/{ref}/location", json={})
    assert r.status_code == 400
    r = await client.post(f"/staff/api/customers/{uuid.uuid4()}/location", json={"lat": LAT, "lng": LNG})
    assert r.status_code == 404

    # washerman: na pin save, na dhoondh — grahak se uska lena-dena nahi
    await _login(client, A_PHONE)
    assert (await client.post(f"/staff/api/customers/{ref}/location", json={"lat": LAT, "lng": LNG})).status_code == 403
    assert (await client.get("/staff/api/customers/locate?q=Pin")).status_code == 403


async def test_owner_switch_hides_pins_from_staff(client, two_shops, sent) -> None:
    """Settings 'Customer address & Route' = Off: pata ke saath pin bhi chhupa."""
    number, ref = await _order_for(two_shops["a"], two_shops["a_del"], CUST)
    await _show_route(two_shops["a"], False)
    await _login(client, A_DEL_PHONE)
    assert (await client.post(f"/staff/api/customers/{ref}/location", json={"lat": LAT, "lng": LNG})).status_code == 200
    stop = next(s for s in (await client.get("/staff/api/route")).json()["stops"] if s["number"] == number)
    assert stop["has_location"] is False and stop["nav_url"] == "" and stop["waypoint"] == ""
    card = next(t for t in (await client.get("/staff/api/tasks?tab=mine")).json()["tasks"] if t["order"]["number"] == number)
    assert "nav_url" not in card["order"]
    assert (await client.get("/staff/api/customers/locate?q=Pin")).status_code == 403


async def test_owner_pastes_a_maps_link_on_the_dashboard(client, sent) -> None:
    await purge_phones(TEST_CUSTOMER_PHONE)
    try:
        home = await tenant_context.get_home_tenant_id()
        async with tenant_context.as_tenant(home):
            async with async_session_factory() as db:
                await create_order(db, customer_phone=TEST_CUSTOMER_PHONE, customer_name="Map Grahak",
                                   items=[{"type": "Shirt", "qty": 1}], created_by="test")

        def row(rows):
            return next(c for c in rows if c["phone"] == TEST_CUSTOMER_PHONE)

        r = await client.put(f"/admin/api/customers/{TEST_CUSTOMER_PHONE}", headers=H,
                             json={"map_url": "https://www.google.com/maps/@25.3176,82.9739,17z"})
        assert r.status_code == 200, r.text
        c = row((await client.get("/admin/api/customers", headers=H)).json())
        assert c["has_location"] and "destination=25.317600,82.973900" in c["nav_url"]
        assert c["map_url"].startswith("https://www.google.com/maps/@")
        assert "address" in c, "edit modal ko address chahiye — list mein aata hi nahi tha"

        r = await client.put(f"/admin/api/customers/{TEST_CUSTOMER_PHONE}", headers=H, json={"map_url": "Lanka gali 3"})
        assert r.status_code == 400
        r = await client.put(f"/admin/api/customers/{TEST_CUSTOMER_PHONE}", headers=H, json={"map_url": ""})
        assert r.status_code == 200
        assert row((await client.get("/admin/api/customers", headers=H)).json())["has_location"] is False
    finally:
        await purge_phones(TEST_CUSTOMER_PHONE)
