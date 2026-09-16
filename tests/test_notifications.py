"""Notification policy + degradation tests, and webhook rule-based replies.

Real WhatsApp is never called — the shared `sent` fixture records sends.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text as sqltext

import app.services.order_service as order_service_module
from app.database import async_session_factory
from app.models import OrderStatus
from app.services.messages import get_message, status_label
from app.services.order_service import (
    create_order,
    set_expected_delivery,
    update_status,
)
from app.services.whatsapp import SendError, WindowClosedError
from tests.conftest import meta_payload, sign_body

PHONE = "+919999900033"
PHONE_RAW = "919999900033"
ITEMS = [{"type": "kurta", "qty": 2, "service": "wash_iron"}]

S = OrderStatus


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    from tests.conftest import purge_phones

    await purge_phones(PHONE)


# --- notification policy ---
# These tests are about what the CUSTOMER hears. Internal traffic (the
# owner's FYI, the delivery boy's "kab tak?") is asserted in test_team.py,
# so filter to this phone instead of counting every message that went out.


def _to_customer(sent) -> list[dict]:
    return [c for c in sent if c["to"] == PHONE]


async def test_create_order_notifies_confirmation(sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
    mine = _to_customer(sent)
    assert len(mine) == 1
    assert order.order_number in mine[0]["text"]


async def test_milestones_notify_but_wash_stages_silent(sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        sent.clear()  # drop the confirmation

        await update_status(db, order, S.IN_WASH, changed_by="test")
        await update_status(db, order, S.IN_DRY, changed_by="test")
        await update_status(db, order, S.IN_IRON, changed_by="test")
        assert _to_customer(sent) == [], "wash/dry/iron must be silent"

        await update_status(db, order, S.READY, changed_by="test")
        mine = _to_customer(sent)
        assert len(mine) == 1 and "ready" in mine[0]["text"]

        await update_status(db, order, S.OUT_FOR_DELIVERY, changed_by="test")
        await update_status(db, order, S.DELIVERED, changed_by="test")
        mine = _to_customer(sent)
        assert len(mine) == 3
        assert "Thank you" in mine[-1]["text"]


async def test_create_notification_has_bill_details(sent) -> None:
    from decimal import Decimal

    async with async_session_factory() as db:
        await create_order(
            db, customer_phone=PHONE, items=ITEMS, created_by="test",
            total_amount=Decimal("350"), advance_hint=Decimal("100"),
        )
    mine = _to_customer(sent)
    assert len(mine) == 1
    text = mine[0]["text"]
    assert "350" in text and "100" in text and "250" in text  # total/advance/due


async def test_create_notification_shows_the_discount(sent) -> None:
    """BUG_002: grahak ko sirf ghata hua total jaata tha, chhoot ka zikr nahi."""
    async with async_session_factory() as db:
        await create_order(
            db, customer_phone=PHONE, items=ITEMS, created_by="test",
            total_amount=Decimal("450"), discount_amount=Decimal("50"),
        )
    text = _to_customer(sent)[0]["text"]
    assert "450" in text and "₹50 discount" in text


async def test_no_discount_line_when_there_is_no_discount(sent) -> None:
    async with async_session_factory() as db:
        await create_order(
            db, customer_phone=PHONE, items=ITEMS, created_by="test",
            total_amount=Decimal("350"),
        )
    assert "discount" not in _to_customer(sent)[0]["text"]


async def _make_coupon(code: str) -> None:
    from app.models import Coupon

    async with async_session_factory() as db:
        db.add(Coupon(code=code, discount_type="percent", value=Decimal("10")))
        await db.commit()


async def _drop_coupon(code: str) -> None:
    async with async_session_factory() as s:
        await s.execute(sqltext("DELETE FROM coupon_redemptions WHERE coupon_code = :c"), {"c": code})
        await s.execute(sqltext("DELETE FROM coupons WHERE code = :c"), {"c": code})
        await s.commit()


async def test_coupon_discount_reaches_the_confirmation_message(sent) -> None:
    """Coupon pehle message jaane KE BAAD lagta tha — grahak ko bina chhoot
    ka total milta tha jabki bill mein chhoot lagi hoti thi."""
    await _make_coupon("TESTBILL10")
    try:
        async with async_session_factory() as db:
            order = await create_order(
                db, customer_phone=PHONE, items=ITEMS, created_by="test",
                total_amount=Decimal("400"), coupon_code="testbill10",
            )
            assert order.total_amount == Decimal("360.00")
            assert order.discount_amount == Decimal("40.00")
            used = (await db.execute(sqltext(
                "SELECT count(*) FROM coupon_redemptions WHERE order_id = :o"
            ), {"o": order.id})).scalar_one()
            assert used == 1
        text = _to_customer(sent)[0]["text"]
        assert "360" in text and "₹40.00 discount" in text
    finally:
        await _drop_coupon("TESTBILL10")


async def test_bad_coupon_creates_no_bill(sent) -> None:
    """Pehle galat coupon par 400 aata tha par bill ban chuka hota tha."""
    from app.services.order_service import OrderError

    async with async_session_factory() as db:
        with pytest.raises(OrderError, match="coupon"):
            await create_order(
                db, customer_phone=PHONE, items=ITEMS, created_by="test",
                total_amount=Decimal("400"), coupon_code="NOSUCHCOUPON",
            )
        n = (await db.execute(sqltext(
            "SELECT count(*) FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.phone = :p"
        ), {"p": PHONE})).scalar_one()
    assert n == 0
    assert _to_customer(sent) == []


async def test_payment_tells_the_customer_but_booking_advance_does_not_double(client, sent) -> None:
    """Paisa aaya to grahak ko raseed. Booking ka advance "order received"
    mein pehle se hai — us par doosra message nahi."""
    from app.config import settings

    auth = {"X-API-Key": settings.ADMIN_API_KEY}
    r = await client.post("/orders", headers=auth, json={
        "customer_phone": PHONE, "customer_name": "Pooja", "total_amount": "500",
        "advance_amount": "100", "items": [{"type": "Saree", "qty": 1}],
    })
    assert r.status_code == 201, r.text
    mine = _to_customer(sent)
    assert len(mine) == 1 and "is received" in mine[0]["text"]   # sirf booking wala

    sent.clear()
    number = r.json()["order_number"]
    r = await client.post(f"/orders/{number}/payment", headers=auth, json={"amount": "250", "method": "UPI"})
    assert r.status_code == 200, r.text
    mine = _to_customer(sent)
    assert len(mine) == 1
    assert "₹250" in mine[0]["text"] and "Balance due: ₹150" in mine[0]["text"] and "Pooja" in mine[0]["text"]

    sent.clear()
    await client.post(f"/orders/{number}/payment", headers=auth, json={"amount": "150", "method": "CASH"})
    assert "fully paid" in _to_customer(sent)[0]["text"]


async def test_partial_delivery_tells_what_came_and_what_is_pending(sent) -> None:
    from app.services import delivery

    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=PHONE, created_by="test",
            items=[{"type": "Shirt", "qty": 12}],
        )
        await update_status(db, order, S.READY, changed_by="test")
        sent.clear()
        await delivery.deliver(db, order, [{"line": 0, "qty": 8}], by="test")
        mine = _to_customer(sent)
        # ek hi message — "out for delivery" nahi, saaf "8 diye, 4 baaki"
        assert len(mine) == 1
        assert "delivered 8 clothes" in mine[0]["text"] and "4 clothes are still with us" in mine[0]["text"]

        sent.clear()
        await delivery.deliver(db, order, None, by="test")
        mine = _to_customer(sent)
        assert len(mine) == 1 and "has been delivered" in mine[0]["text"]   # poora: thank you + rating


async def test_delivered_sends_rating_buttons(sent) -> None:
    from app.models import OrderStatus as S2

    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        await update_status(db, order, S2.READY, changed_by="test")
        sent.clear()
        await update_status(db, order, S2.DELIVERED, changed_by="test")
    assert len(sent) == 1
    assert "Thank you" in sent[0]["text"]
    btns = sent[0].get("buttons") or []
    assert len(btns) == 3 and btns[0].id == "rate_good"


async def test_rating_button_bad_pauses_and_alerts(client, sent) -> None:
    from tests.conftest import meta_payload, sign_body

    async with async_session_factory() as db:
        await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
    sent.clear()
    body = meta_payload(
        messages=[{
            "from": PHONE.removeprefix("+"), "id": "wamid.TESTN-RATE1",
            "type": "interactive",
            "interactive": {"type": "button_reply",
                            "button_reply": {"id": "rate_bad", "title": "😞 Sudhar chahiye"}},
        }]
    )
    r = await client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)})
    assert r.status_code == 200
    texts = [c["text"] or "" for c in sent]
    assert any("sorry" in t for t in texts)          # apology to customer (English)
    assert any("KHARAB RATING" in t for t in texts)  # owner alert stays Hinglish
    async with async_session_factory() as s:
        from sqlalchemy import select as _sel

        from app.models import Customer as _C

        cust = (await s.execute(_sel(_C).where(_C.phone == PHONE))).scalar_one()
        assert cust.agent_paused is True


async def test_date_revision_notifies_without_internal_reason(sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        sent.clear()
        new_date = date.today() + timedelta(days=2)
        await set_expected_delivery(
            db, order, new_date, changed_by="staff:ravi", internal_reason="machine kharab"
        )
    assert len(sent) == 1
    assert "machine kharab" not in sent[0]["text"], "INTERNAL reason leaked to customer!"
    assert new_date.strftime("%d %b %Y") in sent[0]["text"]


async def test_notification_failure_never_breaks_order_update(monkeypatch) -> None:
    async def exploding_send(db, **kwargs):
        raise SendError("meta is down")

    monkeypatch.setattr(order_service_module, "send_message", exploding_send)
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        await update_status(db, order, S.READY, changed_by="test")
    assert order.status is S.READY  # business change survived the send failure


async def test_window_closed_falls_back_to_template(monkeypatch) -> None:
    calls: list[dict] = []

    async def window_closed_then_record(db, *, to_phone, text=None, template_name=None, **kw):
        if text is not None:
            raise WindowClosedError("closed")
        calls.append({"template": template_name, **kw})
        return "wamid.FAKE"

    monkeypatch.setattr(order_service_module, "send_message", window_closed_then_record)
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
    assert len(calls) == 1
    assert calls[0]["template"] == "kk_bill_details"
    assert calls[0]["template_params"][1] == order.order_number  # {{2}} = order


# --- webhook rule-based replies ---

async def _post_text(client, body_text: str, wamid: str):
    body = meta_payload(
        messages=[{"from": PHONE_RAW, "id": wamid, "type": "text", "text": {"body": body_text}}]
    )
    return await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )


async def test_single_active_order_gets_status_reply(client, sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        await update_status(db, order, S.IN_WASH, changed_by="test")
    sent.clear()

    r = await _post_text(client, "mera order kahan hai", "wamid.TESTN-1")
    assert r.status_code == 200
    assert len(sent) == 1
    assert order.order_number in sent[0]["text"]
    assert status_label(S.IN_WASH, "en") in sent[0]["text"]


async def test_order_number_in_message_wins(client, sent) -> None:
    async with async_session_factory() as db:
        o1 = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        o2 = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        await update_status(db, o2, S.READY, changed_by="test")
    sent.clear()

    r = await _post_text(client, f"bhai {o2.order_number.lower()} ka kya hua", "wamid.TESTN-2")
    assert r.status_code == 200
    assert len(sent) == 1
    assert o2.order_number in sent[0]["text"]
    assert o1.order_number not in sent[0]["text"]


async def test_multiple_orders_get_list(client, sent) -> None:
    async with async_session_factory() as db:
        o1 = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        o2 = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
    sent.clear()

    r = await _post_text(client, "status batao", "wamid.TESTN-3")
    assert r.status_code == 200
    assert len(sent) == 1
    assert o1.order_number in sent[0]["text"] and o2.order_number in sent[0]["text"]


async def test_foreign_order_number_not_leaked(client, sent) -> None:
    """Another customer's order number must never reveal their status."""
    async with async_session_factory() as db:
        other = await create_order(
            db, customer_phone="+919999900044", items=ITEMS, created_by="test"
        )
    sent.clear()
    try:
        r = await _post_text(client, f"{other.order_number} kahan hai", "wamid.TESTN-4")
        assert r.status_code == 200
        assert len(sent) == 1
        assert sent[0]["text"] == get_message("order_not_found")
    finally:
        from tests.conftest import purge_phones

        await purge_phones("+919999900044")


async def test_no_orders_falls_back_to_ack(client, sent) -> None:
    r = await _post_text(client, "hello ji", "wamid.TESTN-5")
    assert r.status_code == 200
    assert len(sent) == 1
    assert sent[0]["text"] == get_message("ack_received")


async def test_paused_thread_auto_resumes_after_the_window(client, sent) -> None:
    """Complaint par bot chup hota hai — par HAMESHA ke liye nahi.

    Asli bug: customer ne complaint ki, bot ne khud ko pause kiya, aur
    agle din uska normal sawal ("shop kab khulegi") bhi bina jawab ke
    reh gaya. Ab pause window guzarne par agla message use resume karta hai.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select as _select

    from app.models import Customer
    from tests.conftest import purge_phones

    await purge_phones(PHONE)          # is test ka apna saaf customer chahiye
    async with async_session_factory() as db:
        db.add(Customer(
            phone=PHONE, name="Paused Grahak",
            last_message_at=datetime.now(timezone.utc),
            agent_paused=True,
            agent_paused_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ))
        await db.commit()
    try:
        # 2 ghante purana pause, window 24h -> abhi bhi chup
        import uuid as _uuid

        # unique wamid: journal ka body-hash dedup pichle run se na takraye
        tag = _uuid.uuid4().hex[:8]
        sent.clear()
        r = await _post_text(client, "shop kab khulegi", f"wamid.PAUSE-{tag}-1")
        assert r.status_code == 200
        assert sent == [], "window ke andar bot ko chup rehna chahiye"

        # pause ko 25 ghante purana bana do -> agla message resume kare
        async with async_session_factory() as db:
            c = (
                await db.execute(_select(Customer).where(Customer.phone == PHONE))
            ).scalar_one()
            c.agent_paused_at = datetime.now(timezone.utc) - timedelta(hours=25)
            await db.commit()

        r = await _post_text(client, "shop kab khulegi", f"wamid.PAUSE-{tag}-2")
        assert r.status_code == 200
        assert len(sent) == 1, "window guzar gayi — ab jawab jaana chahiye"

        async with async_session_factory() as db:
            c = (
                await db.execute(_select(Customer).where(Customer.phone == PHONE))
            ).scalar_one()
            assert c.agent_paused is False and c.agent_paused_at is None
    finally:
        await purge_phones(PHONE)
