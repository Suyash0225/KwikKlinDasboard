"""The people side: who is an admin, who hears what, who is asked when.

Owner's rules (06 Aug):
- The owner's number (settings.MANAGER_PHONE) carries owner powers — the
  agent reports orders, payments and problems there, and messages from it
  carry manager powers. Ye .env se aata hai, kisi staff row ke role se
  nahi: role dashboard se kabhi bhi badal sakta hai.
- A customer problem reaches ALL of Suyash, Ravi and Ajit.
- Ajit is the delivery boy: every pickup AND every delivery is asked of him,
  his answer is stored on the task, and the owner is told.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

import app.services.bill_agent as bill_agent
import app.services.tasks as task_service
from app.config import settings
from app.database import async_session_factory
from app.models import (
    Conversation,
    Customer,
    Order,
    OrderStatus,
    PaymentMethod,
    Staff,
    StaffRole,
    Task,
)
from app.services import app_settings, escalation, order_service, team
from tests.conftest import TEST_CUSTOMER_PHONE, purge_phones

# Owner ka number config se aata hai, hardcode nahi — .env badla to test
# apne aap uske saath chalega. Staff ke number to owner dashboard se kabhi
# bhi badal sakta hai, isliye unhe DB se role ke hisaab se dhoondte hain.
OWNER = "+" + settings.MANAGER_PHONE.lstrip("+")
BOY_PHONE = "+919999900089"  # a throwaway delivery boy for the job tests


async def _phones_with_role(db, role: StaffRole) -> set[str]:
    rows = (
        await db.execute(
            select(Staff.phone).where(Staff.role == role, Staff.is_active.is_(True))
        )
    ).scalars().all()
    return {p for p in rows if p}


@pytest.fixture
async def boy(sent):
    """A delivery staff member set as the shop default, cleaned up after."""
    async with async_session_factory() as s:
        st = Staff(
            phone=BOY_PHONE, name="Ajit Test", role=StaffRole.DELIVERY,
            is_active=True, last_message_at=datetime.now(timezone.utc),
        )
        s.add(st)
        await s.commit()
        sid = st.id
        prev = await app_settings.get(s, "default_delivery_phone")
        await app_settings.set_value(s, "default_delivery_phone", BOY_PHONE)

    yield sid

    async with async_session_factory() as s:
        await app_settings.set_value(s, "default_delivery_phone", prev or "")
        await s.execute(delete(Task).where(Task.assigned_staff_id == sid))
        await s.execute(delete(Conversation).where(Conversation.staff_id == sid))
        await s.execute(delete(Staff).where(Staff.id == sid))
        await s.commit()
    await purge_phones(TEST_CUSTOMER_PHONE)


# --- who is who -----------------------------------------------------------


async def test_the_owner_always_carries_owner_powers() -> None:
    """Config ka owner number hamesha owner hai — DB mein kuch bhi ho.

    Pehle ye test owner ke apne STAFF ROW ka role pin karta tha (ADMIN).
    Wo galat pin tha: role dashboard se kabhi bhi badal sakta hai, aur ek
    baar wo galti se badla to ye test toot gaya — jabki system bilkul
    theek chal raha tha. Jo cheez sach mein zaroori hai wo yahi hai: .env
    ka owner number kabhi owner-level powers na khoye, chahe uska staff
    row ho ya na ho.
    """
    async with async_session_factory() as db:
        assert await team.is_admin_phone(db, OWNER)
        assert OWNER in await team.admin_phones(db)


async def test_an_admin_staff_row_gets_owner_powers() -> None:
    """DB mein ADMIN likha ho to us number ko bhi owner ki taakat milti hai.

    Ye apna aadmi khud banata hai — kisi zinda staff par nirbhar nahi,
    isliye owner staff ka number/role kabhi bhi badle to bhi ye sach rehta
    hai.
    """
    phone = "+919999900093"
    async with async_session_factory() as db:
        db.add(Staff(phone=phone, name="Test Admin", role=StaffRole.ADMIN, is_active=True))
        await db.commit()
    try:
        async with async_session_factory() as db:
            assert await team.is_admin_phone(db, phone)
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Staff).where(Staff.phone == phone))
            await db.commit()


async def test_a_worker_is_not_an_admin() -> None:
    """Koi bhi non-admin staff owner nahi hai.

    Owner ka apna number is jaanch se bahar hai: use taakat .env se milti
    hai, uske staff row ke role se nahi. Wo jaan-boojh kar aisa hai — owner
    apne aap ko galti se bhi apni hi dukaan se bahar na kar de.
    """
    async with async_session_factory() as db:
        workers = [
            st
            for st in (
                await db.execute(select(Staff).where(Staff.role != StaffRole.ADMIN))
            ).scalars().all()
            if st.phone != OWNER
        ]
        assert workers, "kam se kam ek worker staff mein hona chahiye"
        for st in workers:
            assert not await team.is_admin_phone(db, st.phone), \
                f"{st.name} ko owner-level powers nahi milni chahiye"


async def test_admin_keeps_manager_powers_in_the_webhook(monkeypatch, sent) -> None:
    """A staff row must not demote the owner to a washerman."""
    import app.routers.webhook as webhook_module

    seen: dict = {}

    async def fake_staff_message(db, *, sender_phone, sender_label, text):
        seen.update(phone=sender_phone, label=sender_label)
        return None

    monkeypatch.setattr(webhook_module, "handle_staff_message", fake_staff_message)
    async with async_session_factory() as db:
        await webhook_module._handle_inbound_message(
            {
                "id": "wamid.TESTadmin1",
                "from": OWNER.lstrip("+"),
                "type": "text",
                "text": {"body": "aaj kitna kaam hua"},
            },
            db,
        )
    assert seen.get("label") == "manager", f"admin ko manager mana jana chahiye: {seen}"
    # and the role scoping in bill_agent leaves an admin unrestricted
    assert bill_agent._ROLE_STATUSES.get("ADMIN") is None


# --- a customer problem reaches everyone ----------------------------------


async def test_escalation_reaches_the_owner_and_the_washerman(sent) -> None:
    """Owner's rule (revised 06 Aug): a customer problem goes to the people
    who can answer it — the owner and the washermen. The delivery boy was
    getting escalations he could do nothing about."""
    async with async_session_factory() as db:
        # SUPERVISOR ("Washerman / Manager") bhi dhulai wala hi hai —
        # customer ki dikkat uske paas bhi jaani chahiye.
        washers = (
            await _phones_with_role(db, StaffRole.WASHER)
            | await _phones_with_role(db, StaffRole.SUPERVISOR)
        )
        delivery_only = await _phones_with_role(db, StaffRole.DELIVERY) - washers
        cust = Customer(phone=TEST_CUSTOMER_PHONE, name="Pareshan Grahak")
        db.add(cust)
        await db.commit()
        await escalation.raise_escalation(
            db, question="mera order kahan hai, koi jawab nahi de raha", customer=cust
        )
    try:
        reached = {c["to"] for c in sent}
        assert washers, "kam se kam ek washer hona chahiye"
        assert ({OWNER} | washers) <= reached, f"sab tak nahi pahuncha: {reached}"
        assert not (delivery_only & reached),             "delivery boy ko customer escalation nahi jani chahiye"
        body = next(c["text"] for c in sent if c["to"] in washers)
        assert "Pareshan Grahak" in body and "koi jawab nahi" in body
    finally:
        await purge_phones(TEST_CUSTOMER_PHONE)


async def test_alert_list_has_no_duplicates() -> None:
    async with async_session_factory() as db:
        phones = [p for p, _ in await team.alert_recipients(db)]
    assert len(phones) == len(set(phones))


# --- the owner hears about money and orders --------------------------------


async def test_new_order_and_payment_are_reported_to_the_owner(sent) -> None:
    try:
        async with async_session_factory() as db:
            order = await order_service.create_order(
                db, customer_phone=TEST_CUSTOMER_PHONE, customer_name="Naya Grahak",
                items=[{"type": "Kurta", "qty": 2}], total_amount=Decimal("200"),
                created_by="agent",
            )
            to_owner = [c["text"] for c in sent if c["to"] == OWNER]
            assert any(order.order_number in t and "Naya order" in t for t in to_owner)

            sent.clear()
            await order_service.record_payment(
                db, order, amount=Decimal("150"), method=PaymentMethod.CASH,
                recorded_by="Ravi",
            )
        to_owner = [c["text"] for c in sent if c["to"] == OWNER]
        assert any("150" in t and "baaki" in t for t in to_owner), to_owner
    finally:
        await purge_phones(TEST_CUSTOMER_PHONE)


async def test_owner_is_not_told_about_his_own_bill(sent) -> None:
    """He just typed it — echoing it back is noise, not service."""
    try:
        async with async_session_factory() as db:
            await order_service.create_order(
                db, customer_phone=TEST_CUSTOMER_PHONE, items=[{"type": "Kurta", "qty": 1}],
                created_by="manager",
            )
        assert not [c for c in sent if c["to"] == OWNER]
    finally:
        await purge_phones(TEST_CUSTOMER_PHONE)


# --- Ajit: pickup AND delivery, always asked, time stored -----------------


async def _ready_order(db) -> Order:
    order = await order_service.create_order(
        db, customer_phone=TEST_CUSTOMER_PHONE, customer_name="Delivery Grahak",
        items=[{"type": "Kurta", "qty": 1}], created_by="agent",
    )
    for s in (OrderStatus.PICKUP_ASSIGNED, OrderStatus.PICKED_UP, OrderStatus.IN_WASH,
              OrderStatus.READY):
        await order_service.update_status(db, order, s, changed_by="Ravi")
    return order


async def test_ready_order_asks_the_delivery_boy_for_a_time(boy, sent) -> None:
    async with async_session_factory() as db:
        order = await _ready_order(db)

    to_boy = [c for c in sent if c["to"] == BOY_PHONE]
    assert to_boy, "delivery boy se pucha hi nahi gaya"
    ask = next(c["text"] for c in to_boy if "Kab tak" in c["text"])
    assert "deliver" in ask.lower() and order.order_number in ask

    async with async_session_factory() as db:
        task = (
            await db.execute(
                select(Task).where(Task.order_id == order.id, Task.kind == "delivery")
            )
        ).scalars().one()
    assert task.status == "OPEN" and task.assigned_staff_id == boy
    assert any(task.code in c["text"] for c in sent if c["to"] == OWNER), "owner ko batao"


async def test_his_delivery_time_is_stored_and_the_owner_told(boy, sent) -> None:
    async with async_session_factory() as db:
        order = await _ready_order(db)
    sent.clear()

    async with async_session_factory() as db:
        reply = await bill_agent.handle_staff_message(
            db, sender_phone=BOY_PHONE, sender_label="Ajit Test",
            text="sham 6 baje tak de dunga",
        )
    assert reply and "sham 6 baje" in reply

    async with async_session_factory() as db:
        task = (
            await db.execute(
                select(Task).where(Task.order_id == order.id, Task.kind == "delivery")
            )
        ).scalars().one()
    assert task.eta_text == "sham 6 baje tak de dunga", "samay DB mein likha jana chahiye"

    owner = [c["text"] for c in sent if c["to"] == OWNER]
    assert any("sham 6 baje" in t for t in owner), owner
    customer = [c["text"] for c in sent if c["to"] == TEST_CUSTOMER_PHONE]
    assert any("sham 6 baje" in t for t in customer), "customer ko bhi samay pata chale"


async def test_yes_on_delivery_marks_the_order_delivered(boy, sent) -> None:
    async with async_session_factory() as db:
        order = await _ready_order(db)
        task = (
            await db.execute(
                select(Task).where(Task.order_id == order.id, Task.kind == "delivery")
            )
        ).scalars().one()
        await task_service.record_pickup_eta(db, task, "sham tak")
    sent.clear()

    async with async_session_factory() as db:
        reply = await bill_agent.handle_staff_message(
            db, sender_phone=BOY_PHONE, sender_label="Ajit Test",
            text="[button:✅ Haan, ho gayi]",
        )
    assert reply and "Shukriya" in reply

    async with async_session_factory() as db:
        fresh = (
            await db.execute(select(Order).where(Order.id == order.id))
        ).scalar_one()
        saved = await task_service.get_by_code(db, task.code)
    assert fresh.status is OrderStatus.DELIVERED
    assert saved.status == "DONE"
    assert [c for c in sent if c["to"] == OWNER], "owner ko delivery ki khabar mile"


async def test_one_delivery_task_per_order(boy, sent) -> None:
    """A status flip-flop must not spam him with the same question twice."""
    async with async_session_factory() as db:
        order = await _ready_order(db)
        await task_service.create_delivery_task(db, order)
        rows = (
            await db.execute(
                select(Task).where(Task.order_id == order.id, Task.kind == "delivery")
            )
        ).scalars().all()
    assert len(rows) == 1


async def test_delivery_reminders_go_to_the_boy_not_the_washer(boy, sent) -> None:
    """Pickup/delivery work orders used to land on the WASHER."""
    from app.services.work_orders import resolve_worker

    async with async_session_factory() as db:
        order = await order_service.create_order(
            db, customer_phone=TEST_CUSTOMER_PHONE,
            items=[{"type": "Kurta", "qty": 1}], created_by="agent",
        )
        who = await resolve_worker(db, order, "DELIVERY")
    try:
        assert who is not None and who.phone == BOY_PHONE
    finally:
        await purge_phones(TEST_CUSTOMER_PHONE)
