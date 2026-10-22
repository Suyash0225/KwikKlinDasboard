"""AI ka offer: had code mein (20%, ₹1000+), grahak-locked, dukaan-locked."""

from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text as sqltext

import app.services.ai_agent as agent_module
from app.database import async_session_factory
from app.models import Coupon, Customer, Rate
from app.models.tenant import Tenant
from app.services import ai_offers, self_order, tenant_context
from app.services.marketing_agent import validate_coupon
from tests.conftest import TEST_CUSTOMER_PHONE, purge_phones
from tests.test_staff_panel import CUST_A, two_shops  # noqa: F401  (fixture)

OTHER_PHONE = "+919999900078"


@pytest.fixture(autouse=True)
async def _clean():
    yield
    async with async_session_factory() as db:
        async with tenant_context.system_context(db):
            await db.execute(sqltext(
                "DELETE FROM coupon_redemptions WHERE coupon_code LIKE 'AI%' AND customer_id IN "
                f"(SELECT id FROM customers WHERE phone IN ('{TEST_CUSTOMER_PHONE}', '{OTHER_PHONE}', '{CUST_A}'))"))
            await db.execute(sqltext(
                "DELETE FROM coupons WHERE code LIKE 'AI%' AND customer_id IN "
                f"(SELECT id FROM customers WHERE phone IN ('{TEST_CUSTOMER_PHONE}', '{OTHER_PHONE}', '{CUST_A}'))"))
            await db.commit()
    await purge_phones(OTHER_PHONE)


async def _customers():
    async with async_session_factory() as db:
        a = Customer(phone=TEST_CUSTOMER_PHONE, name="Offer Grahak")
        b = Customer(phone=OTHER_PHONE, name="Doosra Grahak")
        db.add_all([a, b])
        await db.commit()
        return a, b


def test_clamp_never_goes_past_the_owners_cap() -> None:
    assert ai_offers.clamp(50) == 20
    assert ai_offers.clamp(2) == 5
    assert ai_offers.clamp(0) == 0
    assert ai_offers.clamp("x") == 0
    assert ai_offers.clamp(None) == 0


async def test_offer_is_locked_to_customer_min_1000_and_once() -> None:
    a, b = await _customers()
    async with async_session_factory() as db:
        c = await ai_offers.give(db, a, 35)
        assert c is not None and c.code.startswith("AI")
        assert int(c.value) == 20 and c.min_order == Decimal("1000")
        assert c.customer_id == a.id and c.total_limit == 1

        # dobara maanga = wahi coupon, naya nahi
        again = await ai_offers.give(db, a, 10)
        assert again.code == c.code

        _, cut, err = await validate_coupon(db, c.code, a.id, Decimal("1500"))
        assert err == "" and cut == Decimal("300")
        _, _, err = await validate_coupon(db, c.code, a.id, Decimal("900"))
        assert "minimum" in err
        _, _, err = await validate_coupon(db, c.code, b.id, Decimal("1500"))
        assert "kisi aur" in err                      # forward kiya hua code nahi chalta

        # istemaal ho gaya -> 30 din tak naya offer nahi
        from app.models import CouponRedemption, Order
        from app.services import order_service

        order = await order_service.create_order(
            db, customer_phone=TEST_CUSTOMER_PHONE, items=[{"type": "Shirt", "qty": 1}], created_by="test",
            notify_customer=False,
        )
        db.add(CouponRedemption(tenant_id=c.tenant_id, coupon_code=c.code, order_id=order.id,
                                customer_id=a.id, discount_applied=Decimal("10")))
        await db.commit()
        assert await ai_offers.give(db, a, 10) is None
        assert "not available" in ai_offers.facts_line(await ai_offers.status(db, a))
        # doosre grahak ka apna, alag offer
        other = await ai_offers.give(db, b, 10)
        assert other is not None and other.code != c.code and other.customer_id == b.id


async def test_shop_b_cannot_use_shop_a_offer(client, two_shops) -> None:  # noqa: F811
    async with async_session_factory() as db:
        async with tenant_context.system_context(db):
            slug_b = (await db.get(Tenant, two_shops["b"])).slug
    token = tenant_context.current_tenant_id.set(two_shops["a"])
    try:
        async with async_session_factory() as db:
            cust = Customer(phone=CUST_A, name="Shop A Grahak")
            db.add(cust)
            await db.commit()
            coupon = await ai_offers.give(db, cust, 15)
            code = coupon.code
    finally:
        tenant_context.current_tenant_id.reset(token)
    token = tenant_context.current_tenant_id.set(two_shops["b"])
    try:
        async with async_session_factory() as db:
            db.add(Rate(service="ZzOfferSvc", garment="Saree", unit="pc", rate=Decimal("600")))
            await db.commit()
    finally:
        tenant_context.current_tenant_id.reset(token)
    try:
        r = await client.post("/billcreation/api/coupon", json={
            "s": slug_b, "code": code, "items": [{"service": "ZzOfferSvc", "garment": "Saree", "qty": 2}]})
        assert r.status_code == 400                   # doosri dukaan ka coupon yahan hai hi nahi
    finally:
        async with async_session_factory() as db:
            async with tenant_context.system_context(db):
                await db.execute(delete(Rate).where(Rate.service == "ZzOfferSvc"))
                await db.execute(delete(Coupon).where(Coupon.code == code))
                await db.commit()
        await purge_phones(CUST_A)


async def test_ai_reply_appends_real_coupon_capped_at_20(monkeypatch) -> None:
    a, _ = await _customers()

    async def fake_ask_json(**kw):
        return {
            "reply": "Offer Grahak ji, aap hamare purane customer hain — aapke liye kuch khaas! 😊",
            "intent": "PRICE_QUERY", "language": "hi", "action": "ANSWER", "action_reason": "",
            "escalate": False, "escalation_reason": "", "admin_note": "",
            "offer_percent": 40,
            "intake": {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False},
        }

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        reply = await agent_module.build_ai_reply(db, a, "koi discount milega? 6 saree dry clean karani hai")
        coupon = (await db.execute(select(Coupon).where(Coupon.customer_id == a.id))).scalar_one()
    assert int(coupon.value) == 20
    assert coupon.code in reply and "20%" in reply and "1000" in reply
    assert self_order.PATH in reply                   # order page ka apna link bhi


async def test_facts_tell_the_model_whether_it_may_offer() -> None:
    a, _ = await _customers()
    async with async_session_factory() as db:
        assert "AI offer available" in ai_offers.facts_line(await ai_offers.status(db, a))
        c = await ai_offers.give(db, a, 10)
        line = ai_offers.facts_line(await ai_offers.status(db, a))
    assert c.code in line and "already given" in line


async def test_personal_offer_on_plain_link_says_use_your_link(client) -> None:
    a, _ = await _customers()
    async with async_session_factory() as db:
        db.add(Rate(service="ZzPlainSvc", garment="Saree", unit="pc", rate=Decimal("600")))
        await db.commit()
        c = await ai_offers.give(db, a, 10)
    try:
        items = [{"service": "ZzPlainSvc", "garment": "Saree", "qty": 2}]
        r = await client.post("/billcreation/api/coupon", json={"code": c.code, "items": items})
        assert r.status_code == 400 and "personal offer" in r.json()["detail"]
        home = await tenant_context.get_home_tenant_id()
        r = await client.post("/billcreation/api/coupon", json={
            "t": self_order.make_token(home, a.id), "code": c.code, "items": items})
        assert r.status_code == 200 and r.json()["discount"] == 120.0
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Rate).where(Rate.service == "ZzPlainSvc"))
            await db.commit()
