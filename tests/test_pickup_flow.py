"""The pickup conversation the agent runs on its own.

A WhatsApp order used to end with "Delivery: jald batayenge" and the owner
remembering to arrange a pickup. Now: the boy is asked when, the customer
is told that time and his number, and he confirms with one tap.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text as sqltext

import app.services.bill_agent as bill_agent
import app.services.tasks as task_service
from app.config import settings
from app.database import async_session_factory
from app.models import Conversation, Order, OrderStatus, Staff, StaffRole, Task
from app.services import app_settings, order_service
from tests.conftest import TEST_CUSTOMER_PHONE, purge_phones

PICKUP_STAFF_PHONE = "+919999900087"


@pytest.fixture
async def boy(sent):
    """An active delivery staff member, set as the shop default.

    One session in, one session out — nesting more async fixtures around a
    live engine is what produced 'event loop is closed' at teardown.
    """
    async with async_session_factory() as s:
        st = Staff(
            phone=PICKUP_STAFF_PHONE, name="Ajitram", role=StaffRole.DELIVERY,
            is_active=True, last_message_at=datetime.now(timezone.utc),
        )
        s.add(st)
        await s.commit()
        sid = st.id
        prev = await app_settings.get(s, "default_delivery_phone")
        await app_settings.set_value(s, "default_delivery_phone", PICKUP_STAFF_PHONE)

    yield sid

    async with async_session_factory() as s:
        await app_settings.set_value(s, "default_delivery_phone", prev or "")
        await s.execute(delete(Task).where(Task.assigned_staff_id == sid))
        await s.execute(delete(Conversation).where(Conversation.staff_id == sid))
        await s.execute(delete(Staff).where(Staff.id == sid))
        await s.commit()
    await purge_phones(TEST_CUSTOMER_PHONE)


async def _new_pickup():
    """Book a WhatsApp order and let the agent raise its pickup task."""
    async with async_session_factory() as db:
        order = await order_service.create_order(
            db, customer_phone=TEST_CUSTOMER_PHONE, customer_name="Archana Sharma",
            items=[{"type": "Chandni", "qty": 1}], created_by="agent",
        )
        task = await task_service.create_pickup_task(db, order)
        return task, order.order_number


async def test_pickup_task_asks_the_boy_and_tells_the_owner(boy, sent) -> None:
    task, num = await _new_pickup()
    assert task is not None and task.kind == "pickup"

    to_boy = [c for c in sent if c["to"] == PICKUP_STAFF_PHONE]
    assert to_boy, "the delivery boy must be asked"
    assert "Kab tak" in to_boy[0]["text"]
    assert num in to_boy[0]["text"]
    assert "Archana Sharma" in to_boy[0]["text"]

    # he also gets a "naya order" FYI first — the pickup line is what matters
    to_owner = [c for c in sent if c["to"] == settings.MANAGER_PHONE]
    assert to_owner, "the owner is told it has been arranged"
    assert any(task.code in c["text"] for c in to_owner)


async def test_boys_answer_becomes_the_customers_promise(boy, sent) -> None:
    """'sham tak' -> customer hears 'sham tak' plus his number."""
    task, num = await _new_pickup()
    sent.clear()

    async with async_session_factory() as db:
        reply = await bill_agent.handle_staff_message(
            db, sender_phone=PICKUP_STAFF_PHONE, sender_label="Ajitram",
            text="sham tak kar dunga",
        )
    assert reply and "sham tak" in reply

    async with async_session_factory() as db:
        saved = await task_service.get_by_code(db, task.code)
    assert saved.eta_text == "sham tak kar dunga", "the promise is stored, not guessed"

    to_customer = [c for c in sent if c["to"] == TEST_CUSTOMER_PHONE]
    assert to_customer, "the customer must be told the time"
    body = to_customer[0]["text"]
    assert "sham tak" in body
    assert "Ajitram" in body and PICKUP_STAFF_PHONE in body, "name AND number"
    assert num in body

    # and he is asked to confirm, with buttons
    with_buttons = [c for c in sent if c.get("buttons")]
    assert with_buttons, "Yes/No buttons must follow"
    titles = [b.title for b in with_buttons[0]["buttons"]]
    assert any("Haan" in t for t in titles) and any("Abhi nahi" in t for t in titles)


async def test_chatter_is_not_mistaken_for_a_time(boy, sent) -> None:
    """Only a message that looks like a time answers the question."""
    task, _ = await _new_pickup()
    async with async_session_factory() as db:
        await bill_agent.handle_staff_message(
            db, sender_phone=PICKUP_STAFF_PHONE, sender_label="Ajitram",
            text="theek hai bhaiya",
        )
    async with async_session_factory() as db:
        saved = await task_service.get_by_code(db, task.code)
    assert saved.eta_text is None


async def test_yes_button_closes_task_and_moves_the_order(boy, sent) -> None:
    task, num = await _new_pickup()
    async with async_session_factory() as db:
        t = await task_service.get_by_code(db, task.code)
        await task_service.record_pickup_eta(db, t, "sham tak")
    sent.clear()

    async with async_session_factory() as db:
        reply = await bill_agent.handle_staff_message(
            db, sender_phone=PICKUP_STAFF_PHONE, sender_label="Ajitram",
            text="[button:✅ Haan, ho gaya]",
        )
    assert reply and "Shukriya" in reply

    async with async_session_factory() as db:
        saved = await task_service.get_by_code(db, task.code)
        assert saved.status == "DONE"
        order = (
            await db.execute(select(Order).where(Order.order_number == num))
        ).scalar_one()
        assert order.status is OrderStatus.PICKED_UP
        assert order.expected_delivery is not None, "SLA clock starts at pickup"

    assert [c for c in sent if c["to"] == settings.MANAGER_PHONE], "owner is told"


async def test_no_button_keeps_it_open(boy, sent) -> None:
    task, _ = await _new_pickup()
    async with async_session_factory() as db:
        t = await task_service.get_by_code(db, task.code)
        await task_service.record_pickup_eta(db, t, "sham tak")
    sent.clear()

    async with async_session_factory() as db:
        reply = await bill_agent.handle_staff_message(
            db, sender_phone=PICKUP_STAFF_PHONE, sender_label="Ajitram",
            text="[button:❌ Abhi nahi]",
        )
    assert reply and "batana" in reply
    async with async_session_factory() as db:
        saved = await task_service.get_by_code(db, task.code)
    assert saved.status == "OPEN", "not done means still pending"
    assert [c for c in sent if c["to"] == settings.MANAGER_PHONE], "owner hears about it"


async def test_unpriced_order_skips_the_rupee_dashes(sent) -> None:
    """'Total: rupee-dash | Baaki: rupee-dash' read as broken; say it plainly."""
    try:
        async with async_session_factory() as db:
            await order_service.create_order(
                db, customer_phone=TEST_CUSTOMER_PHONE, customer_name="Bina Rate",
                items=[{"type": "Chandni", "qty": 1}], created_by="agent",
            )
        body = next(c["text"] for c in sent if c["to"] == TEST_CUSTOMER_PHONE)
        assert "₹—" not in body
        assert "bill bana ke bhej denge" in body
    finally:
        await purge_phones(TEST_CUSTOMER_PHONE)


async def test_priced_order_still_shows_the_money(sent) -> None:
    try:
        async with async_session_factory() as db:
            await order_service.create_order(
                db, customer_phone=TEST_CUSTOMER_PHONE, customer_name="Rate Wala",
                items=[{"type": "shirt", "qty": 2}], total_amount=Decimal("300"),
                created_by="agent",
            )
        body = next(c["text"] for c in sent if c["to"] == TEST_CUSTOMER_PHONE)
        assert "300" in body and "Total" in body
    finally:
        await purge_phones(TEST_CUSTOMER_PHONE)


# --- Ravi "ready" bole -> Ajit tak pahunchna CHAHIYE ----------------------


async def test_delivery_ask_reaches_ajit_even_with_a_closed_window(
    monkeypatch, sent
) -> None:
    """Asli bug (09 Aug): order READY hua, T-14 bana, owner ko gaya
    "Ajit ko de di, samay pooch liya hai" — lekin Ajit ki 24h chat band
    thi aur ask chupchaap gir gaya. Ajit ko kabhi pata hi nahi chala.
    """
    from app.database import async_session_factory
    from app.models import Staff, StaffRole
    from app.services import tasks as task_service
    from app.services.order_service import create_order
    from app.services.whatsapp import WindowClosedError

    phone = "+919999900093"
    cust = "+919999900079"
    async with async_session_factory() as db:
        db.add(Staff(phone=phone, name="Ajittest", role=StaffRole.DELIVERY, is_active=True))
        await db.commit()
        prior = await app_settings.get(db, "default_delivery_phone")
        await app_settings.set_value(db, "default_delivery_phone", phone)

    calls: list[dict] = []

    async def window_shut(db, *, to_phone, text=None, template_name=None, **kw):
        calls.append({"to": to_phone, "text": text, "template_name": template_name, **kw})
        if template_name is None:
            raise WindowClosedError("24h window closed")
        return "wamid.TPL"

    monkeypatch.setattr(task_service, "send_message", window_shut)
    try:
        async with async_session_factory() as db:
            order = await create_order(
                db, customer_phone=cust, customer_name="Hold Grahak",
                items=[{"type": "Lehenga", "qty": 1}], created_by="test",
            )
            task = await task_service.create_delivery_task(db, order)
        assert task is not None
        to_ajit = [c for c in calls if c["to"] == phone]
        assert to_ajit, "Ajit ko kuch to jana hi chahiye"
        assert any(c["template_name"] == "kk_staff_alert" for c in to_ajit), \
            "chat band ho to template se jana chahiye — chupchaap girna nahi"
    finally:
        async with async_session_factory() as db:
            await app_settings.set_value(db, "default_delivery_phone", prior or "")
            await db.execute(sqltext(
                "UPDATE tasks SET assigned_staff_id = NULL WHERE assigned_staff_id IN "
                "(SELECT id FROM staff WHERE phone = :p)"), {"p": phone})
            await db.execute(sqltext("DELETE FROM staff WHERE phone = :p"), {"p": phone})
            await db.commit()
        from tests.conftest import purge_phones

        await purge_phones(cust)


async def test_delivery_ask_asks_in_his_own_words(monkeypatch, sent) -> None:
    """"Kab tak?" par koi chhapa hua option nahi.

    Owner ka faisla (09 Aug): delivery wala samay haath ka kaam dekh kar
    batata hai — "1-2 ghante / sham tak / kal" jaise buttons na uske kaam
    se milte hain, na owner ko sach dikhate hain. Baaki jagah (ho gaya /
    time lagega / dikkat hai) buttons rehte hain, kyunki wahan jawab
    gine-chune hain.
    """
    from app.database import async_session_factory
    from app.models import Staff, StaffRole
    from app.services import tasks as task_service
    from app.services.order_service import create_order

    phone = "+919999900092"
    cust = "+919999900076"
    async with async_session_factory() as db:
        db.add(Staff(phone=phone, name="Ajitdo", role=StaffRole.DELIVERY, is_active=True))
        await db.commit()
        prior = await app_settings.get(db, "default_delivery_phone")
        await app_settings.set_value(db, "default_delivery_phone", phone)
    try:
        async with async_session_factory() as db:
            order = await create_order(
                db, customer_phone=cust, customer_name="Btn Grahak",
                items=[{"type": "Kurta", "qty": 1}], created_by="test",
            )
            await task_service.create_delivery_task(db, order)
        ask = next(c for c in sent if c["to"] == phone and "Kab tak" in (c["text"] or ""))
        assert not ask.get("buttons"), "samay ke liye chhapa hua option nahi dena"
        assert "jaise" in ask["text"], "misaal se samajhna aasan rahe"
    finally:
        async with async_session_factory() as db:
            await app_settings.set_value(db, "default_delivery_phone", prior or "")
            await db.execute(sqltext(
                "UPDATE tasks SET assigned_staff_id = NULL WHERE assigned_staff_id IN "
                "(SELECT id FROM staff WHERE phone = :p)"), {"p": phone})
            await db.execute(sqltext("DELETE FROM staff WHERE phone = :p"), {"p": phone})
            await db.commit()
        from tests.conftest import purge_phones

        await purge_phones(cust)
