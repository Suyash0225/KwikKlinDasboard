"""Loyalty rewards: niyam -> inaam apne aap -> bill par ek tap -> cancel; aur
pichhle bill ka baaki (paisa + kapde) naye bill par.

Suraksha: reward coupon sirf usi grahak par; doosri dukaan ke niyam/reward
kabhi nahi dikhte (RLS + ORM filter).
"""

from decimal import Decimal

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models import Customer, Order, OrderStatus, PaymentMethod
from app.services import bill_link, order_service, rewards, tenant_context
from tests.conftest import purge_phones
from tests.test_staff_panel import two_shops  # noqa: F401 — fixture

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
PHONE = "+919999900091"
OTHER = "+919999900092"
ITEM = [{"type": "Shirt", "service": "Wash", "qty": 1, "rate": 100, "amount": 100}]


async def _wipe_rewards(home) -> None:
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM customer_rewards"))
            await db.execute(sqltext("DELETE FROM coupons WHERE code LIKE 'RW-%'"))
            await db.execute(sqltext("DELETE FROM reward_rules"))
            await db.commit()


@pytest.fixture
async def clean(sent):
    home = await tenant_context.get_home_tenant_id()
    await _wipe_rewards(home)
    await purge_phones(PHONE, OTHER)
    yield home
    await purge_phones(PHONE, OTHER)
    await _wipe_rewards(home)


async def _order(phone, total="100", coupon=None, items=None):
    async with async_session_factory() as db:
        return await order_service.create_order(
            db, customer_phone=phone, customer_name="Reward Grahak", created_by="test",
            items=items or ITEM, total_amount=Decimal(total), coupon_code=coupon,
        )


async def test_rule_earns_a_customer_locked_coupon_and_it_applies_once(client, clean, sent, monkeypatch) -> None:
    home = clean
    # reward ki khabar whatsapp module se seedha jaati hai — use bhi record karo
    import app.services.whatsapp as _wa

    async def fake(db, *, to_phone, text=None, **kw):
        sent.append({"to": to_phone, "text": text, **kw}); return "wamid.R"

    monkeypatch.setattr(_wa, "send_message", fake)
    r = await client.post("/admin/api/rewards/rules", headers=AUTH, json={
        "name": "2 bills", "kind": "bills", "window_days": 30, "threshold": 2,
        "reward_type": "flat", "value": 50, "min_order": 0, "valid_days": 30,
    })
    assert r.status_code == 201, r.text
    assert (await client.get("/admin/api/rewards/rules", headers=AUTH)).json()["rules"][0]["reward"] == "₹50 off"

    await _order(PHONE)
    assert (await client.get("/admin/api/customers/rewards", headers=AUTH, params={"phone": PHONE})).json()["available"] == []
    sent.clear()
    await _order(PHONE)                                   # doosra bill -> niyam poora
    got = (await client.get("/admin/api/customers/rewards", headers=AUTH, params={"phone": PHONE})).json()
    assert len(got["available"]) == 1 and got["available"][0]["reward"] == "₹50 off"
    code = got["available"][0]["code"]
    assert code.startswith("RW-") and got["progress"][0]["done"] is True
    # grahak ko khabar
    assert any(code in (c.get("text") or "") for c in sent if c["to"] == PHONE)

    # doosre grahak par nahi chalta
    with pytest.raises(order_service.OrderError):
        await _order(OTHER, coupon=code)
    # apne par: chhoot lagi, reward used
    o = await _order(PHONE, total="200", coupon=code)
    assert o.total_amount == Decimal("150") and o.discount_amount == Decimal("50")
    earned = (await client.get("/admin/api/rewards/earned", headers=AUTH)).json()["rewards"]
    mine = next(x for x in earned if x["code"] == code)
    assert mine["status"] == "used"
    # dobara nahi
    with pytest.raises(order_service.OrderError):
        await _order(PHONE, coupon=code)
    # window mein dobara inaam nahi (3rd/4th bill par phir se nahi)
    got = (await client.get("/admin/api/customers/rewards", headers=AUTH, params={"phone": PHONE})).json()
    assert got["available"] == []



async def test_owner_can_cancel_a_reward_and_percent_cap_applies(client, clean, sent) -> None:
    r = await client.post("/admin/api/rewards/rules", headers=AUTH, json={
        "name": "spend 100", "kind": "spend", "window_days": 90, "threshold": 100,
        "reward_type": "percent", "value": 10, "max_discount": 15, "valid_days": 10,
    })
    assert r.status_code == 201, r.text
    await _order(PHONE, total="120")
    got = (await client.get("/admin/api/customers/rewards", headers=AUTH, params={"phone": PHONE})).json()
    assert got["available"] and got["available"][0]["reward"] == "10% off (max ₹15)"
    code = got["available"][0]["code"]
    # cap: 10% of 500 = 50 -> 15
    async with async_session_factory() as db:
        from app.models import Customer as _C
        from app.services.marketing_agent import validate_coupon

        cust = (await db.execute(sqltext("SELECT id FROM customers WHERE phone = :p"), {"p": PHONE})).scalar_one()
        coupon, disc, err = await validate_coupon(db, code, cust, Decimal("500"))
        assert err == "" and disc == Decimal("15")

    rid = got["available"][0]["id"]
    assert (await client.post(f"/admin/api/rewards/earned/{rid}/cancel", headers=AUTH)).json()["status"] == "cancelled"
    with pytest.raises(order_service.OrderError):
        await _order(PHONE, coupon=code)
    assert (await client.get("/admin/api/customers/rewards", headers=AUTH, params={"phone": PHONE})).json()["available"] == []


async def test_new_bill_shows_earlier_money_and_clothes_still_at_shop(client, clean, sent, monkeypatch) -> None:
    """Pichhla bill: ₹100 baaki + 2 kapde dukaan par -> naye bill ke text aur
    web page par saaf; page se sab-bill wale statement ka link."""
    from app.services import delivery

    home = clean
    monkeypatch.setattr(settings, "SITE_URL", "https://kwikklin.online")
    a = await _order(PHONE, total="100", items=[{"type": "Kurta", "service": "Wash", "qty": 3, "rate": 30, "amount": 90}])
    async with async_session_factory() as db:
        oa = await db.get(Order, a.id)
        await order_service.update_status(db, oa, OrderStatus.READY, changed_by="test", notify=False)
        await delivery.deliver(db, oa, [{"line": 0, "qty": 1}], by="test")      # 2 baaki
    b = await _order(PHONE, total="60")

    text_b = (await client.get(f"/orders/{b.order_number}/receipt", headers=AUTH)).json()["text"]
    assert "Previous due (1 bill): ₹100" in text_b
    assert "Still with us from earlier bills (2):" in text_b and f"{a.order_number} Kurta: 2" in text_b

    html = (await client.get(f"/b/{bill_link.make(home, b.id)}")).text
    assert "Earlier bills" in html and "Still with us" in html and a.order_number in html
    assert "See all bills" in html and "/b/c/" in html

    # statement: pending dono + rewards card (rule ho to) — yahan koi rule nahi, card nahi
    async with async_session_factory() as db:
        cust = (await db.execute(sqltext("SELECT id FROM customers WHERE phone = :p"), {"p": PHONE})).scalar_one()
    st = (await client.get(f"/b/c/{bill_link.make_customer(home, cust)}")).text
    assert a.order_number in st and b.order_number in st and "Your rewards" not in st
    # paid ho gaya -> pending se hat kar "Last 90 days" history mein
    async with async_session_factory() as db:
        ob = await db.get(Order, b.id)
        await order_service.record_payment(db, ob, amount=Decimal("60"), method=PaymentMethod.CASH,
                                           recorded_by="test", notify_customer=False)
    st = (await client.get(f"/b/c/{bill_link.make_customer(home, cust)}")).text
    assert "Last 90 days" in st and "Paid ✓" in st and "1 bill pending" in st


async def test_rewards_stay_inside_their_shop(client, clean, two_shops, sent) -> None:
    """Dukaan A ka niyam/reward dukaan B ko kabhi nahi dikhta."""
    async with tenant_context.as_tenant(two_shops["a"]):
        async with async_session_factory() as db:
            from app.models import RewardRule

            db.add(RewardRule(name="A rule", kind="bills", window_days=30, threshold=1,
                              reward_type="flat", value=10, valid_days=10))
            await db.commit()
            assert len(await rewards.active_rules(db)) == 1
    async with tenant_context.as_tenant(two_shops["b"]):
        async with async_session_factory() as db:
            assert await rewards.active_rules(db) == []
    await _wipe_rewards(two_shops["a"])
