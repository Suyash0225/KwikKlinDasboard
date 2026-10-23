"""📍 Grahak se location ek baar maangna, aur WhatsApp par aayi location ko
pin ki tarah save karna (services/location_request.py + webhook)."""

import uuid

from sqlalchemy import select

from app.database import async_session_factory
from app.models import Customer
from app.routers.webhook import _extract_text, _waha_location, _waha_to_meta_shape
from app.services import geo, location_request, tenant_context
from app.services.order_service import create_order
from tests.conftest import TEST_CUSTOMER_PHONE, purge_phones
from tests.test_webhook import _post_waha

NOWEB = {"_data": {"message": {"locationMessage": {
    "degreesLatitude": 25.3176, "degreesLongitude": 82.9739, "name": "Ghar", "address": "Lanka, Varanasi",
}}}}


def test_waha_location_shapes_become_a_readable_pin() -> None:
    """NOWEB (Baileys) aur WEBJS dono — pehle pin body ki khali line ban jaata tha."""
    m = _waha_to_meta_shape({"from": "919999900299@c.us", "id": "x", "body": "", **NOWEB})
    assert m["type"] == "location"
    assert _extract_text(m) == "[location:25.3176,82.9739] Ghar · Lanka, Varanasi"
    assert _waha_location({"location": {"latitude": "25.1", "longitude": "82.2", "description": "Office"}}) == {
        "latitude": 25.1, "longitude": 82.2, "name": "Office", "address": "",
    }
    assert _waha_location({"body": "hi"}) is None
    assert _waha_location({"location": {"latitude": "abc"}}) is None


async def test_shared_location_is_saved_as_the_pin_without_ai(client, sent, monkeypatch) -> None:
    import app.services.llm_client as llm

    async def no_ai(**kw):
        raise AssertionError("a shared pin must not cost an LLM call")

    monkeypatch.setattr(llm, "ask_json", no_ai)
    phone = f"+91996{uuid.uuid4().int % 10**7:07d}"
    r = await _post_waha(client, monkeypatch, "message", {
        "id": f"waha-loc-{uuid.uuid4().hex}", "fromMe": False, "from": f"{phone[1:]}@c.us", "body": "", **NOWEB,
    })
    assert r.status_code == 200
    async with async_session_factory() as db:
        cust = (await db.execute(select(Customer).where(Customer.phone == phone))).scalar_one()
        assert geo.payload(cust)["has_location"] and cust.location_by == "customer"
        assert float(cust.lat) == 25.3176 and float(cust.lng) == 82.9739
        assert cust.address == "Ghar · Lanka, Varanasi", "pata khali tha — pin ke saath bhar gaya"
    thanks = [m for m in sent if m["to"] == phone]
    assert len(thanks) == 1 and "📍" in thanks[0]["text"]
    await purge_phones(phone)


async def test_pickup_order_asks_for_location_exactly_once(sent, monkeypatch) -> None:
    await purge_phones(TEST_CUSTOMER_PHONE)
    asked = []

    async def fake_send(db, *, to_phone, text, **kw):
        asked.append({"to": to_phone, "text": text, **kw})
        return "wamid.FAKELOC"

    monkeypatch.setattr(location_request, "send_message", fake_send)
    home = await tenant_context.get_home_tenant_id()
    try:
        async with tenant_context.as_tenant(home):
            async with async_session_factory() as db:
                await create_order(db, customer_phone=TEST_CUSTOMER_PHONE, customer_name="Pin Poochho",
                                   items=[{"type": "Shirt", "qty": 2}], created_by="test", needs_pickup=True)
                assert len(asked) == 1 and asked[0]["to"] == TEST_CUSTOMER_PHONE
                assert "location" in asked[0]["text"].lower() and asked[0]["intent"] == "transactional"

                # doosra pickup: dobara nahi — ek request kaafi hai
                await create_order(db, customer_phone=TEST_CUSTOMER_PHONE, items=[{"type": "Pant", "qty": 1}],
                                   created_by="test", needs_pickup=True)
                assert len(asked) == 1
                cust = (await db.execute(select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE))).scalar_one()
                assert cust.location_asked_at is not None

                # pin aa gaya -> poochne ki zaroorat hi nahi; STOP wale ko kabhi nahi
                cust.location_asked_at = None
                geo.set_location(cust, lat=25.3, lng=82.9, by="customer")
                assert location_request.should_ask(cust) is False
                cust.lat = cust.lng = None
                cust.opted_out = True
                assert location_request.should_ask(cust) is False
                cust.opted_out = False
                assert location_request.should_ask(cust) is True
                assert await location_request.ask_once(db, cust, why="test") is True
                assert len(asked) == 2
    finally:
        await purge_phones(TEST_CUSTOMER_PHONE)
