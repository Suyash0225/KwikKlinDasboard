"""/billcreation — grahak ka apna order: token wala link, saada link + WhatsApp code."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.database import async_session_factory
from app.models import Customer, Order, OrderStatus, Rate
from app.services import self_order, tenant_context
from tests.conftest import TEST_CUSTOMER_PHONE, TEST_CUSTOMER_PHONE_RAW
from tests.test_staff_panel import two_shops  # noqa: F401  (fixture)
from tests.test_webhook import _post_waha

SERVICE = "Zzselforder Wash"
KG_SERVICE = "Zzselforder Fold"


@pytest.fixture
async def rates():
    async with async_session_factory() as db:
        db.add(Rate(service=SERVICE, garment="Shirt", unit="pc", rate=Decimal("30")))
        db.add(Rate(service=SERVICE, garment="Saree", unit="pc", rate=Decimal("120")))
        db.add(Rate(service=KG_SERVICE, garment="", unit="kg", rate=Decimal("80")))
        await db.commit()
    yield
    async with async_session_factory() as db:
        await db.execute(delete(Rate).where(Rate.service.in_((SERVICE, KG_SERVICE))))
        await db.commit()


@pytest.fixture
async def customer():
    async with async_session_factory() as db:
        c = Customer(phone=TEST_CUSTOMER_PHONE, name="Web Grahak", address="12 Lanka, near BHU gate")
        db.add(c)
        await db.commit()
        await db.refresh(c)
        return c


@pytest.fixture
def wa_sent(monkeypatch):
    """Router send_message andar se import karta hai — wahi patch."""
    import app.services.whatsapp as wa

    calls = []

    async def fake(db, *, to_phone, text=None, **kw):
        calls.append({"to": to_phone, "text": text})
        return "wamid.FAKE"

    monkeypatch.setattr(wa, "send_message", fake)
    return calls


def _tomorrow() -> str:
    return (self_order._today() + timedelta(days=1)).isoformat()


def test_token_roundtrip_tamper_and_expiry() -> None:
    t, c = uuid.uuid4(), uuid.uuid4()
    tok = self_order.make_token(t, c, now=1000)
    assert self_order.parse_token(tok, now=1001) == (t, c)
    assert self_order.parse_token(tok[:-2] + "AA", now=1001) is None
    assert self_order.parse_token(tok, now=1000 + self_order.TOKEN_TTL_SECONDS + 5) is None
    assert self_order.parse_token("garbage") is None
    # bill ka token yahan nahi chalta (alag key, alag lambai)
    from app.services import bill_link

    assert self_order.parse_token(bill_link.make_customer(t, c)) is None


def test_code_is_found_in_the_whatsapp_text() -> None:
    text = self_order.whatsapp_text("K7M2QX", [
        {"qty": 2.0, "unit": "pc", "type": "Shirt", "service": SERVICE},
    ], Decimal("60"), self_order._today())
    assert self_order.find_code(text) == "K7M2QX"
    assert self_order.find_code("order code: k7m2qx") == "K7M2QX"
    assert self_order.find_code("mera order kab aayega") is None


async def test_page_and_plain_form_use_home_rate_card(client, rates) -> None:
    r = await client.get("/billcreation")
    assert r.status_code == 200 and "billcreation.js" in r.text
    assert r.headers["x-robots-tag"] == "noindex"
    form = (await client.get("/billcreation/api/form")).json()
    assert {"service": SERVICE, "garment": "Shirt", "unit": "pc", "rate": 30.0} in form["rates"]
    assert form["customer"] is None


async def test_token_link_places_order_with_customers_number(client, rates, customer, wa_sent) -> None:
    home = await tenant_context.get_home_tenant_id()
    tok = self_order.make_token(home, customer.id)
    form = (await client.get(f"/billcreation/api/form?t={tok}")).json()
    assert form["customer"]["name"] == "Web Grahak"
    assert form["customer"]["phone_masked"].endswith(TEST_CUSTOMER_PHONE[-4:])
    assert TEST_CUSTOMER_PHONE not in str(form)

    body = {
        "t": tok, "name": "Web Grahak", "address": "", "pickup_date": _tomorrow(),
        "items": [
            {"service": SERVICE, "garment": "Shirt", "qty": 3},
            {"service": KG_SERVICE, "garment": "", "qty": 2.5},
        ],
    }
    r = await client.post("/billcreation/api/order", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "placed" and out["estimate"] == 290.0     # 3×30 + 2.5×80

    async with async_session_factory() as db:
        order = (await db.execute(select(Order).where(Order.order_number == out["order_number"]))).scalar_one()
    assert order.status is OrderStatus.PICKUP_ASSIGNED
    assert order.total_amount is None                  # asli bill pickup par
    assert [(i["garment"], i["qty"], i["rate"]) for i in order.items] == [("Shirt", 3.0, 30.0), ("", 2.5, 80.0)]
    assert "Customer self-order" in order.notes and "₹290" in order.notes
    assert any(c["to"] == TEST_CUSTOMER_PHONE and out["order_number"] in c["text"] for c in wa_sent)

    # dohra tap = wahi order, naya nahi
    again = (await client.post("/billcreation/api/order", json=body)).json()
    assert again["order_number"] == out["order_number"]


async def test_rates_come_from_card_not_from_the_browser(client, rates, customer, wa_sent) -> None:
    home = await tenant_context.get_home_tenant_id()
    tok = self_order.make_token(home, customer.id)
    r = await client.post("/billcreation/api/order", json={
        "t": tok, "pickup_date": _tomorrow(),
        "items": [{"service": SERVICE, "garment": "Blazer", "qty": 1}],
    })
    assert r.status_code == 400 and "rate card" in r.json()["detail"]
    r = await client.post("/billcreation/api/order", json={
        "t": tok, "pickup_date": "2001-01-01",
        "items": [{"service": SERVICE, "garment": "Shirt", "qty": 1}],
    })
    assert r.status_code == 400
    r = await client.post("/billcreation/api/order", json={
        "t": "bad.token", "pickup_date": _tomorrow(),
        "items": [{"service": SERVICE, "garment": "Shirt", "qty": 1}],
    })
    assert r.status_code == 404


async def test_plain_link_creates_order_from_customers_own_whatsapp(client, rates, sent, monkeypatch) -> None:
    async with async_session_factory() as db:
        from app.services import app_settings

        prev = await app_settings.get(db, "shop_contact_phone")
        await app_settings.set_value(db, "shop_contact_phone", "9696856069")
    self_order._WA_NUMBER.clear()
    monkeypatch.setattr("app.services.waha.enabled", lambda: False)
    try:
        r = await client.post("/billcreation/api/order", json={
            "name": "Naya Grahak", "address": "45 Sigra, near mall", "pickup_date": _tomorrow(),
            "items": [{"service": SERVICE, "garment": "Saree", "qty": 2}],
        })
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["status"] == "confirm_on_whatsapp" and out["whatsapp"] == "919696856069"
        assert f"Order code: {out['code']}" in out["text"]

        # Grahak ke apne number se wahi message aata hai
        resp = await _post_waha(client, monkeypatch, "message", {
            "id": f"wamid.TEST-self-{uuid.uuid4().hex}", "fromMe": False,
            "from": f"{TEST_CUSTOMER_PHONE_RAW}@c.us", "body": out["text"],
        })
        assert resp.status_code == 200
        async with async_session_factory() as db:
            cust = (await db.execute(select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE))).scalar_one()
            orders = (await db.execute(select(Order).where(Order.customer_id == cust.id))).scalars().all()
        assert len(orders) == 1
        assert orders[0].status is OrderStatus.PICKUP_ASSIGNED and orders[0].items[0]["garment"] == "Saree"
        assert cust.address == "45 Sigra, near mall"
        assert any(c["to"] == TEST_CUSTOMER_PHONE and orders[0].order_number in (c["text"] or "") for c in sent)

        # Code ek hi baar chalta hai
        home = await tenant_context.get_home_tenant_id()
        assert self_order.take_draft(home, out["code"]) is None
    finally:
        async with async_session_factory() as db:
            await app_settings.set_value(db, "shop_contact_phone", prev or "")
        self_order._WA_NUMBER.clear()


async def test_place_order_prices_from_card_and_summarises(rates, customer) -> None:
    async with async_session_factory() as db:
        items, est = await self_order.price_items(db, [{"service": SERVICE, "garment": "Shirt", "qty": 4}])
        order, created = await self_order.place_order(
            db, customer_phone=TEST_CUSTOMER_PHONE, name="Web Grahak", address="", items=items,
            estimate=est, pickup_day=self_order._today(), via="test",
        )
    assert created and est == Decimal("120")
    from app.services.work_orders import items_summary

    assert items_summary(order) == "4 x Shirt"


@pytest.fixture
async def coupon():
    from app.models import Coupon

    async with async_session_factory() as db:
        db.add(Coupon(code="ZZSELF10", discount_type="percent", value=Decimal("10"), min_order=Decimal("100")))
        await db.commit()
    yield "ZZSELF10"
    async with async_session_factory() as db:
        await db.execute(delete(Coupon).where(Coupon.code == "ZZSELF10"))
        await db.commit()


async def test_coupon_shows_discount_and_reaches_pickup_bill(client, rates, customer, coupon, wa_sent) -> None:
    home = await tenant_context.get_home_tenant_id()
    tok = self_order.make_token(home, customer.id)
    items = [{"service": SERVICE, "garment": "Saree", "qty": 2}]        # 240
    r = await client.post("/billcreation/api/coupon", json={"t": tok, "code": "zzself10", "items": items})
    assert r.status_code == 200, r.text
    assert r.json() == {"code": "ZZSELF10", "discount": 24.0, "estimate": 216.0}

    # min order se kam = saaf galti
    r = await client.post("/billcreation/api/coupon", json={
        "t": tok, "code": coupon, "items": [{"service": SERVICE, "garment": "Shirt", "qty": 1}]})
    assert r.status_code == 400 and "ZZSELF10" in r.json()["detail"]
    r = await client.post("/billcreation/api/coupon", json={"t": tok, "code": "NOPE", "items": items})
    assert r.status_code == 400

    out = (await client.post("/billcreation/api/order", json={
        "t": tok, "pickup_date": _tomorrow(), "coupon": coupon, "items": items})).json()
    assert out["status"] == "placed" and out["estimate"] == 216.0
    async with async_session_factory() as db:
        order = (await db.execute(select(Order).where(Order.order_number == out["order_number"]))).scalar_one()
    assert order.total_amount is None                     # redeem pickup ke asli bill par
    from app.routers.staff_panel import _self_order_coupon

    assert _self_order_coupon(order.notes) == "ZZSELF10"
    assert any("ZZSELF10" in (c["text"] or "") for c in wa_sent)


async def test_order_with_invalid_coupon_is_refused(client, rates, customer) -> None:
    home = await tenant_context.get_home_tenant_id()
    r = await client.post("/billcreation/api/order", json={
        "t": self_order.make_token(home, customer.id), "pickup_date": _tomorrow(), "coupon": "NOPE",
        "items": [{"service": SERVICE, "garment": "Shirt", "qty": 1}]})
    assert r.status_code == 400


async def test_order_is_placed_even_when_whatsapp_is_down(client, rates, customer, monkeypatch) -> None:
    """WAHA down: owner/grahak ko message nahi gaya, par order bana aur page ko 200."""
    import app.services.whatsapp as wa
    from app.services import waha

    async def down(*a, **kw):
        raise waha.WahaError("WAHA network error", transient=True)

    async def down_meta(*a, **kw):
        raise wa.SendError("down", transient=True)

    monkeypatch.setattr(waha, "_post", down)
    monkeypatch.setattr(wa, "_post_with_retry", down_meta)
    home = await tenant_context.get_home_tenant_id()
    r = await client.post("/billcreation/api/order", json={
        "t": self_order.make_token(home, customer.id), "pickup_date": _tomorrow(),
        "items": [{"service": SERVICE, "garment": "Shirt", "qty": 2}]})
    assert r.status_code == 200, r.text
    async with async_session_factory() as db:
        assert (await db.execute(select(Order).where(Order.order_number == r.json()["order_number"]))).scalar_one()


async def test_dashboard_order_link_is_plain_for_home_and_slug_for_others(client, monkeypatch, two_shops) -> None:  # noqa: F811
    from app.models.tenant import Tenant
    from app.routers import agent_admin
    from app.services import google_auth

    async def base(db):
        return "https://kwikklin.online"

    monkeypatch.setattr(google_auth, "public_base", base)
    home = await tenant_context.get_home_tenant_id()
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            assert (await agent_admin.order_link(db))["url"] == "https://kwikklin.online/billcreation"
    async with tenant_context.as_tenant(two_shops["b"]):
        async with async_session_factory() as db:
            slug = (await db.get(Tenant, two_shops["b"])).slug
            assert (await agent_admin.order_link(db))["url"] == f"https://kwikklin.online/billcreation?s={slug}"
