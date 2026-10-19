"""Assigned tasks: the agent gives work, chases it, and records the answer.

The whole point is that "Ravi se bol do X" stops being a message that
scrolls away and becomes something with an owner and a status.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

import app.services.bill_agent as bill_agent
import app.services.tasks as task_service
from app.services import team
from app.config import settings
from app.database import async_session_factory
from app.models import TASK_DONE, TASK_OPEN, Conversation, OrderStatus, Staff, StaffRole, Task
from app.services.order_service import create_order
from tests.conftest import purge_phones

H = {"X-API-Key": settings.ADMIN_API_KEY}
TASK_STAFF_PHONE = "+919999900085"


@pytest.fixture
def awake(monkeypatch):
    """Follow-ups are suppressed during quiet hours (21:00-09:00 IST). The
    tests must assert the LOGIC, not what time the suite happens to run."""
    monkeypatch.setattr(task_service, "_in_quiet_hours", lambda _now: False)


@pytest.fixture
async def worker():
    async with async_session_factory() as s:
        st = Staff(
            phone=TASK_STAFF_PHONE, name="Taskram", role=StaffRole.WASHER,
            is_active=True, last_message_at=datetime.now(timezone.utc),
        )
        s.add(st)
        await s.commit()
        sid = st.id
    yield sid
    async with async_session_factory() as s:
        await s.execute(delete(Task).where(Task.assigned_staff_id == sid))
        await s.execute(delete(Conversation).where(Conversation.staff_id == sid))
        await s.execute(delete(Staff).where(Staff.id == sid))
        await s.commit()


async def _mk(db, staff, title="Sharma ji ka order aaj deliver karna hai", **kw):
    st = await db.get(Staff, staff)
    return await task_service.create_task(db, title=title, staff=st, **kw)


# --- creating ---

async def test_create_task_messages_the_assignee(worker, sent) -> None:
    async with async_session_factory() as db:
        task = await _mk(db, worker)
    assert task.code.startswith("T-")
    assert task.status == TASK_OPEN
    assert sent and sent[0]["to"] == TASK_STAFF_PHONE
    body = sent[0]["text"]
    assert task.code in body and "Sharma ji" in body
    assert "current task status" in body.lower(), "staff must be given a clear status menu"
    assert "Due:" not in body
    assert "Bill & payment:" not in body


async def test_owner_saying_bol_do_creates_a_tracked_task(worker, sent, monkeypatch) -> None:
    """'Ravi se bol do ...' must become a task, not a message that vanishes."""
    async def fake_extract(db, text, pending, history):
        return {
            "action": "relay", "relay_to": "Taskram",
            "relay_message": "Sharma ji ka order jaldi chahiye, aaj hi nikal do",
            "items": [], "order_number": "", "new_date": "", "reason": "",
            "new_status": "NONE", "customer_name": "", "customer_phone": "",
            "advance": 0, "priority": "", "staff_name": "", "note": "",
            "amount": 0, "method": "", "done_refs": [], "pending_refs": [],
            "problem": "",
        }

    monkeypatch.setattr(bill_agent, "_extract", fake_extract)
    async with async_session_factory() as db:
        reply = await bill_agent.handle_staff_message(
            db, sender_phone=settings.MANAGER_PHONE, sender_label="manager",
            text="Taskram se bol do Sharma ji ka order jaldi chahiye",
        )
    assert reply and "T-" in reply

    async with async_session_factory() as db:
        task = (
            await db.execute(select(Task).where(Task.assigned_staff_id == worker))
        ).scalars().first()
    assert task is not None, "the instruction must be tracked"
    assert task.urgent is True, "'jaldi' should tighten the follow-up clock"
    assert "pucho" not in task.title.lower(), "task must address the worker directly"
    assert any(c["to"] == TASK_STAFF_PHONE for c in sent)


async def test_staff_free_text_issue_does_not_update_task(worker, sent) -> None:
    """Free text may not attach an issue note or mutate a task."""
    async with async_session_factory() as db:
        task = await _mk(db, worker, title="Customer pickup")
        code = task.code
    sent.clear()

    async with async_session_factory() as db:
        await bill_agent.handle_staff_message(
            db, sender_phone=TASK_STAFF_PHONE, sender_label="Taskram",
            text="Kapde geele hain, pickup nahi ho payega",
        )

    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
    assert task.status == TASK_OPEN
    assert task.reply is None
    assert task.completed_at is None
    assert not any("Task issue" in (message["text"] or "") for message in sent)


# --- completing ---

async def test_typed_done_code_does_not_close_task(worker, sent) -> None:
    """A typed task code is not a substitute for an explicit task button."""
    async with async_session_factory() as db:
        task = await _mk(db, worker)
        code = task.code

    async with async_session_factory() as db:
        await bill_agent.handle_staff_message(
            db, sender_phone=TASK_STAFF_PHONE, sender_label="Taskram",
            text=f"done {code}",
        )

    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
        assert task.status == TASK_OPEN
        assert task.completed_at is None
        assert task.reply is None


async def test_unassigned_staff_cannot_update_task_with_button(worker, sent) -> None:
    """Only the assignee can update; matching role/team is not authorization."""
    other_phone = "+919999900089"
    async with async_session_factory() as db:
        other = Staff(
            phone=other_phone, name="Taskram Two", role=StaffRole.WASHER,
            is_active=True, last_message_at=datetime.now(timezone.utc),
        )
        db.add(other)
        await db.commit()
        task = await _mk(db, worker, title="Assigned-only task", notify=False)
        code = task.code
        other_id = other.id

    try:
        async with async_session_factory() as db:
            visible = await task_service.open_tasks_for_staff(db, other_id)
            assert all(t.code != code for t in visible)
            reply = await bill_agent.handle_staff_message(
                db, sender_phone=other_phone, sender_label="Taskram Two",
                text=f"[button:task:{code}:done] Done",
            )
            assert "assigned" in (reply or "").lower()

        async with async_session_factory() as db:
            task = await task_service.get_by_code(db, code)
            assert task.status == TASK_OPEN
            assert task.completed_at is None
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.code == code))
            await db.execute(delete(Staff).where(Staff.id == other_id))
            await db.commit()


async def test_assigned_staff_button_updates_task_in_database(worker, sent) -> None:
    """An explicit button from the assignee changes persisted task status."""
    async with async_session_factory() as db:
        task = await _mk(db, worker, title="Button completion", notify=False)
        code = task.code

    async with async_session_factory() as db:
        reply = await bill_agent.handle_staff_message(
            db, sender_phone=TASK_STAFF_PHONE, sender_label="Taskram",
            text=f"[button:task:{code}:done] Done",
        )
        assert reply is not None

    async with async_session_factory() as db:
        saved = await task_service.get_by_code(db, code)
        assert saved.status == TASK_DONE
        assert saved.completed_at is not None


async def test_pending_button_saves_only_its_task_to_database(worker, sent) -> None:
    """A button carries its task code, so multiple open tasks are never guessed."""
    async with async_session_factory() as db:
        target = await _mk(db, worker, title="Target task", notify=False)
        other = await _mk(db, worker, title="Other task", notify=False)
        code, other_code = target.code, other.code

    try:
        async with async_session_factory() as db:
            reply = await bill_agent.handle_staff_message(
                db, sender_phone=TASK_STAFF_PHONE, sender_label="Taskram",
                text=f"[button:task:{code}:pending] Pending",
            )
            assert "pending" in (reply or "").lower()

        async with async_session_factory() as db:
            target = await task_service.get_by_code(db, code)
            other = await task_service.get_by_code(db, other_code)
            assert target.reply == "Pending"
            assert other.reply is None
            assert target.status == TASK_OPEN
            assert other.status == TASK_OPEN
    finally:
        bill_agent._PENDING.pop(TASK_STAFF_PHONE, None)


async def test_unknown_code_is_not_a_crash(worker, sent) -> None:
    async with async_session_factory() as db:
        reply = await bill_agent.handle_staff_message(
            db, sender_phone=TASK_STAFF_PHONE, sender_label="Taskram", text="done T-9999",
        )
    assert reply and "nahi mila" in reply


async def test_free_text_eta_does_not_write_task_database(worker, sent) -> None:
    """Even after a Pending button, free-text ETA cannot mutate task fields."""
    async with async_session_factory() as db:
        task = await _mk(db, worker, title="ETA is dashboard-only", notify=False)
        code = task.code
    bill_agent._PENDING[TASK_STAFF_PHONE] = bill_agent.PendingTaskEta(code=code)
    try:
        async with async_session_factory() as db:
            await bill_agent.handle_staff_message(
                db, sender_phone=TASK_STAFF_PHONE, sender_label="Taskram",
                text="kal 5 baje",
            )
        async with async_session_factory() as db:
            saved = await task_service.get_by_code(db, code)
            assert saved.eta_text is None
            assert saved.reply is None
            assert saved.status == TASK_OPEN
    finally:
        bill_agent._PENDING.pop(TASK_STAFF_PHONE, None)


async def test_free_text_never_updates_task_record(worker, sent) -> None:
    """Natural language may be understood/reported, but cannot write task fields."""
    async with async_session_factory() as db:
        task = await _mk(db, worker)
        code = task.code

    async with async_session_factory() as db:
        await bill_agent.handle_staff_message(
            db, sender_phone=TASK_STAFF_PHONE, sender_label="Taskram",
            text="machine kharab hai, kal karunga",
        )

    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
    assert task.reply is None
    assert task.eta_text is None
    assert task.status == TASK_OPEN


# --- following up ---

async def test_followup_pings_only_when_due(worker, sent, awake) -> None:
    async with async_session_factory() as db:
        task = await _mk(db, worker)
        code = task.code
    sent.clear()

    # Assert on THIS task, not the global sweep count — other tests' tasks
    # may also be open in the shared dev database.
    await task_service.run_task_followups()
    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
    assert task.ping_count == 0, "just created — not due yet"

    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
        task.last_ping_at = datetime.now(timezone.utc) - timedelta(hours=3)
        db.add(task)
        await db.commit()

    await task_service.run_task_followups()
    assert any(code in (c["text"] or "") and "reminder" in (c["text"] or "").lower() for c in sent)

    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
    assert task.ping_count == 1


async def test_silence_escalates_to_the_manager(worker, sent, awake, monkeypatch) -> None:
    # Freeze the scheduler clock at midday so the test never crosses an IST
    # calendar-day boundary when subtracting the reminder gap.
    fixed_now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(task_service, "datetime", FrozenDateTime)
    async with async_session_factory() as db:
        task = await _mk(db, worker)
        code = task.code
        task.created_at = fixed_now - timedelta(hours=10)
        task.ping_count = task_service.ESCALATE_AFTER_PINGS
        task.last_ping_at = fixed_now - timedelta(hours=5)
        db.add(task)
        await db.commit()
    sent.clear()

    await task_service.run_task_followups()
    async with async_session_factory() as db:
        owner_phone = await team.primary_admin_phone(db)
    to_manager = [c for c in sent if c["to"] == owner_phone]
    assert to_manager, "the owner must be told when staff go quiet"
    assert code in to_manager[0]["text"]

    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
    assert task.escalated_at is not None

    sent.clear()
    await task_service.run_task_followups()
    assert not [c for c in sent if c["to"] == owner_phone], "escalate once, not every tick"


async def test_task_followups_max_three_per_day(worker, sent, awake, monkeypatch) -> None:
    """An open task may receive at most three staff reminders per day."""
    # Keep the test timestamp safely inside one IST calendar day.
    fixed_now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(task_service, "datetime", FrozenDateTime)
    async with async_session_factory() as db:
        task = await _mk(db, worker)
        task.created_at = fixed_now - timedelta(hours=10)
        task.last_ping_at = fixed_now - timedelta(hours=3)
        task.ping_count = 3
        db.add(task)
        await db.commit()

    sent.clear()
    await task_service.run_task_followups()

    assert not [c for c in sent if c["to"] == TASK_STAFF_PHONE], (
        "fourth reminder must not be sent on the same day"
    )


async def test_stale_open_delivery_task_is_closed_when_order_is_delivered(sent, awake) -> None:
    """A completed delivery must never keep receiving scheduler reminders."""
    from app.services.order_service import create_order

    phone = "+919999900090"
    async with async_session_factory() as db:
        staff = Staff(
            phone=phone, name="Delivery Guard", role=StaffRole.DELIVERY,
            is_active=True, last_message_at=datetime.now(timezone.utc),
        )
        db.add(staff)
        await db.commit()
        order = await create_order(
            db, customer_phone="+919999900091", customer_name="Delivered Customer",
            items=[{"type": "Shirt", "qty": 1}], created_by="test",
        )
        order.status = OrderStatus.DELIVERED
        task = Task(
            code=await task_service._next_code(db),
            title="Delivered Customer ko delivery karni hai",
            assigned_staff_id=staff.id, order_id=order.id, kind="delivery",
            created_by="test",
            last_ping_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        db.add(task)
        await db.commit()
        code = task.code

    try:
        sent.clear()
        await task_service.run_task_followups()
        async with async_session_factory() as db:
            updated = await task_service.get_by_code(db, code)
            assert updated.status == TASK_DONE
            assert updated.completed_at is not None
        assert not any(code in (m.get("text") or "") and "reminder" in (m.get("text") or "").lower() for m in sent)
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.code == code))
            await db.execute(delete(Staff).where(Staff.id == staff.id))
            await db.commit()


async def test_stale_open_pickup_task_is_closed_after_pickup(sent, awake) -> None:
    """Once pickup is recorded, an old pickup task cannot be chased again."""
    from app.services.order_service import create_order

    phone = "+919999900092"
    async with async_session_factory() as db:
        staff = Staff(
            phone=phone, name="Pickup Guard", role=StaffRole.DELIVERY,
            is_active=True, last_message_at=datetime.now(timezone.utc),
        )
        db.add(staff)
        await db.commit()
        order = await create_order(
            db, customer_phone="+919999900093", customer_name="Picked Customer",
            items=[{"type": "Shirt", "qty": 1}], created_by="test",
        )
        order.status = OrderStatus.PICKED_UP
        task = Task(
            code=await task_service._next_code(db),
            title="Picked Customer se pickup karna hai",
            assigned_staff_id=staff.id, order_id=order.id, kind="pickup",
            created_by="test",
            last_ping_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        db.add(task)
        await db.commit()
        code = task.code

    try:
        sent.clear()
        await task_service.run_task_followups()
        async with async_session_factory() as db:
            updated = await task_service.get_by_code(db, code)
            assert updated.status == TASK_DONE
        assert not any(code in (m.get("text") or "") and "reminder" in (m.get("text") or "").lower() for m in sent)
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.code == code))
            await db.execute(delete(Staff).where(Staff.id == staff.id))
            await db.commit()


async def test_done_tasks_are_left_alone(worker, sent, awake) -> None:
    async with async_session_factory() as db:
        task = await _mk(db, worker)
        task.last_ping_at = datetime.now(timezone.utc) - timedelta(hours=9)
        db.add(task)
        await db.commit()
        await task_service.complete_task(db, task, by="Taskram")
    sent.clear()
    assert await task_service.run_task_followups() == 0


# --- dashboard API ---

async def test_api_create_list_and_close(client, worker, sent) -> None:
    r = await client.post("/admin/api/tasks", headers=H, json={
        "title": "Dhulai wali machine saaf kar dena", "staff": "Taskram", "urgent": True,
    })
    assert r.status_code == 201
    code = r.json()["code"]

    r = await client.get("/admin/api/tasks?status=OPEN", headers=H)
    row = next(t for t in r.json() if t["code"] == code)
    assert row["staff"] == "Taskram"
    assert row["urgent"] is True
    assert row["status"] == "OPEN"

    assert (await client.post(f"/admin/api/tasks/{code}/done", headers=H)).status_code == 200
    r = await client.get("/admin/api/tasks?status=DONE", headers=H)
    assert any(t["code"] == code for t in r.json())


async def test_api_rejects_unknown_staff(client) -> None:
    r = await client.post("/admin/api/tasks", headers=H, json={
        "title": "kuch bhi", "staff": "KoiAisaBandaNahiHai",
    })
    assert r.status_code == 400


# ---------------------------------------------------- task buttons -------

async def test_task_status_menu_enforces_sequential_wash_dry_iron_ready(client, sent, worker) -> None:
    """WhatsApp must only allow the next legal processing stage."""
    import uuid as _uuid
    from tests.conftest import meta_payload, sign_body

    async def post(text):
        tag = _uuid.uuid4().hex[:8]
        body = meta_payload(messages=[{
            "from": TASK_STAFF_PHONE.lstrip("+"), "id": f"wamid.TM-{tag}",
            "type": "text", "text": {"body": text},
        }])
        return await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
        )

    phone = f"+919999{uuid.uuid4().int % 1_000_000:06d}"
    delivery_phone = f"+919997{uuid.uuid4().int % 1_000_000:06d}"
    async with async_session_factory() as db:
        delivery_staff = Staff(
            phone=delivery_phone,
            name="Sequential Flow Delivery",
            role=StaffRole.DELIVERY,
            is_active=True,
            last_message_at=datetime.now(timezone.utc),
        )
        db.add(delivery_staff)
        await db.flush()
        from app.services.order_service import create_order
        order = await create_order(
            db,
            customer_phone=phone,
            customer_name="Sharma ji",
            created_by="test",
            items=[{"type": "Shirt", "qty": 2}],
        )
        task = await _mk(db, worker, title="Sharma ji ka wash", order=order, kind="wash")
        code, order_id = task.code, order.id

    try:
        menu_messages = [c for c in sent if c.get("list_rows")]
        assert menu_messages, "task assignment must contain a list menu"
        rows = menu_messages[-1]["list_rows"]
        assert [row.id for row in rows] == [
            f"task:{code}:wash",
            f"task:{code}:pending",
        ]

        # A stale card must not jump directly from RECEIVED to READY.
        await post(f"[button:task:{code}:ready] Ready")
        async with async_session_factory() as db:
            current = await db.get(type(order), order_id)
            assert current.status is OrderStatus.RECEIVED

        # Start washing, then explicitly move the same wash task into drying.
        await post(f"[button:task:{code}:wash] Wash")
        async with async_session_factory() as db:
            current = await db.get(type(order), order_id)
            assert current.status is OrderStatus.IN_WASH
            current_task = await task_service.get_by_code(db, code)
            assert current_task.status == TASK_OPEN

        await post(f"[button:task:{code}:dry] Washing done")
        async with async_session_factory() as db:
            current = await db.get(type(order), order_id)
            assert current.status is OrderStatus.IN_DRY
            completed_wash = await task_service.get_by_code(db, code)
            assert completed_wash.status == TASK_DONE
            dry_task = (await db.execute(
                select(Task).where(
                    Task.order_id == order_id, Task.kind == "dry", Task.status == TASK_OPEN
                ).order_by(Task.created_at.desc())
            )).scalars().first()
            assert dry_task is not None, "IN_DRY must create the drying-stage task"
            dry_code = dry_task.code

        await post(f"[button:task:{dry_code}:iron] Drying done")
        async with async_session_factory() as db:
            current = await db.get(type(order), order_id)
            assert current.status is OrderStatus.IN_IRON
            iron_task = (await db.execute(
                select(Task).where(
                    Task.order_id == order_id, Task.kind == "iron", Task.status == TASK_OPEN
                ).order_by(Task.created_at.desc())
            )).scalars().first()
            assert iron_task is not None, "IN_IRON must create the ironing-stage task"
            iron_code = iron_task.code

        await post(f"[button:task:{iron_code}:ready] Ironing done")
        async with async_session_factory() as db:
            current = await db.get(type(order), order_id)
            assert current.status is OrderStatus.READY
            completed_iron = await task_service.get_by_code(db, iron_code)
            assert completed_iron.status == TASK_DONE
            delivery_task = (await db.execute(
                select(Task).where(
                    Task.order_id == order_id, Task.kind == "delivery", Task.status == TASK_OPEN
                )
            )).scalars().first()
            assert delivery_task is not None, "READY must create the delivery task"
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.order_id == order_id))
            await db.commit()
        from tests.conftest import purge_phones
        await purge_phones(phone, delivery_phone)


async def test_daily_briefing_pick_uses_stage_menu_for_linked_wash_task(worker, sent) -> None:
    """Picking a wash task from standup must not expose generic Done."""
    phone = f"+919996{uuid.uuid4().int % 1_000_000:06d}"
    async with async_session_factory() as db:
        order = await create_order(
            db,
            customer_phone=phone,
            customer_name="Briefing Menu Test",
            created_by="test",
            items=[{"type": "Shirt", "qty": 1}],
        )
        task = await _mk(
            db, worker, title="Briefing menu wash", order=order, kind="wash"
        )
        code, order_id = task.code, order.id

    sent.clear()
    try:
        async with async_session_factory() as db:
            reply = await bill_agent._handle_work_pick(
                db, TASK_STAFF_PHONE, "Taskram", f"[button:pick:t:{code}]"
            )

        assert reply == ""
        menus = [message for message in sent if message.get("list_rows")]
        assert menus, "selecting a briefing task should send an interactive stage menu"
        rows = menus[-1]["list_rows"]
        ids = [row.id for row in rows]
        assert ids == [f"task:{code}:wash", f"task:{code}:pending"]
        assert f"task:{code}:done" not in ids

        async with async_session_factory() as db:
            current_order = await db.get(type(order), order_id)
            current_task = await task_service.get_by_code(db, code)
            assert current_order.status is OrderStatus.RECEIVED
            assert current_task.status == TASK_OPEN
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.order_id == order_id))
            await db.commit()
        await purge_phones(phone)


async def test_delivery_done_requires_two_confirmations(worker, sent) -> None:
    """First Done means out for delivery; only a second Done confirms delivery."""
    from app.models import Customer
    from app.services.order_service import create_order
    from tests.conftest import purge_phones

    phone = f"+919999{uuid.uuid4().int % 1_000_000:06d}"
    async with async_session_factory() as db:
        delivery_staff = Staff(
            phone=f"+919998{uuid.uuid4().int % 1_000_000:06d}",
            name="Delivery Flow Test",
            role=StaffRole.DELIVERY,
            is_active=True,
            last_message_at=datetime.now(timezone.utc),
        )
        db.add(delivery_staff)
        await db.commit()
        staff_id = delivery_staff.id
        order = await create_order(
            db,
            customer_phone=phone,
            customer_name="Delivery Flow Customer",
            created_by="test",
            items=[{"type": "Shirt", "qty": 1}],
        )
        # Start at the exact stage where the delivery task is actionable.
        order.status = OrderStatus.READY
        db.add(order)
        await db.commit()
        task = await _mk(
            db, staff_id, title="Deliver test order",
            order=order, kind="delivery", notify=False,
        )
        code, order_id = task.code, order.id

    try:
        sent.clear()
        async with async_session_factory() as db:
            reply = await bill_agent.handle_staff_message(
                db, sender_phone=delivery_staff.phone, sender_label="Delivery Flow Test",
                text=f"[button:task:{code}:done] Done",
            )
        assert "out for delivery" in (reply or "").lower()

        async with async_session_factory() as db:
            current_order = await db.get(type(order), order_id)
            current_task = await task_service.get_by_code(db, code)
            assert current_order.status is OrderStatus.OUT_FOR_DELIVERY
            assert current_task.status == TASK_OPEN, "task must remain open until hand-off"
        customer_messages = [m.get("text", "").lower() for m in sent if m.get("to") == phone]
        assert any("out for delivery" in m for m in customer_messages)
        assert not any("has been delivered" in m or "order delivered" in m for m in customer_messages)

        sent.clear()
        async with async_session_factory() as db:
            reply = await bill_agent.handle_staff_message(
                db, sender_phone=delivery_staff.phone, sender_label="Delivery Flow Test",
                text=f"[button:task:{code}:done] Done",
            )
        assert "customer updated" in (reply or "").lower() or "delivered" in (reply or "").lower()

        async with async_session_factory() as db:
            current_order = await db.get(type(order), order_id)
            current_task = await task_service.get_by_code(db, code)
            assert current_order.status is OrderStatus.DELIVERED
            assert current_task.status == TASK_DONE
        customer_messages = [m.get("text", "").lower() for m in sent if m.get("to") == phone]
        assert any("delivered" in m for m in customer_messages)
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.code == code))
            await db.execute(delete(Staff).where(Staff.id == staff_id))
            await db.commit()
        await purge_phones(phone)


async def test_delivery_task_uses_done_pending_menu(sent) -> None:
    """Pickup and delivery work use the same simple Done/Pending menu."""
    async with async_session_factory() as db:
        delivery_phone = f"+919999{uuid.uuid4().int % 1_000_000:06d}"
        worker = Staff(
            phone=delivery_phone, name="Delivery Test", role=StaffRole.DELIVERY,
            is_active=True, last_message_at=datetime.now(timezone.utc),
        )
        db.add(worker)
        await db.commit()
        sid = worker.id
        from app.models import Customer
        customer = Customer(phone=f"+919999{uuid.uuid4().int % 1_000_000:06d}", name="Rahul ji", is_active=True)
        db.add(customer)
        await db.commit()
        task = await _mk(
            db, sid, title="Rahul ji ka pickup complete karna hai",
            customer=customer, kind="pickup"
        )
    try:
        menu_messages = [c for c in sent if c.get("list_rows") and c.get("to") == delivery_phone]
        assert menu_messages
        rows = menu_messages[-1]["list_rows"]
        assert [row.id for row in rows] == [
            f"task:{task.code}:done", f"task:{task.code}:pending"
        ]
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.assigned_staff_id == sid))
            await db.execute(delete(Staff).where(Staff.id == sid))
            await db.execute(delete(Customer).where(Customer.id == customer.id))
            await db.commit()


# --- number/naam badalna turant asar kare ---------------------------------


async def test_find_staff_ignores_deactivated_people(worker) -> None:
    """Band kiye gaye aadmi ko naya kaam nahi jana chahiye."""
    async with async_session_factory() as db:
        assert await task_service.find_staff(db, "Taskram") is not None
        st = await db.get(Staff, worker)
        st.is_active = False
        await db.commit()
        assert await task_service.find_staff(db, "Taskram") is None
        assert await task_service.find_staff(db, TASK_STAFF_PHONE) is None


async def test_exact_name_wins_over_a_lookalike(worker) -> None:
    """Do milte-julte naam ho to poora naam bolne par agent haar na maane.

    Pehle koi bhi do match milte hi 'staff list mein nahi mila' aa jata tha,
    chahe owner ne poora naam bilkul theek likha ho.
    """
    async with async_session_factory() as db:
        twin = Staff(
            phone="+919999900086", name="Taskrampal", role=StaffRole.WASHER,
            is_active=True,
        )
        db.add(twin)
        await db.commit()
        twin_id = twin.id
    try:
        async with async_session_factory() as db:
            found = await task_service.find_staff(db, "Taskram")
            assert found is not None and found.id == worker
            other = await task_service.find_staff(db, "Taskrampal")
            assert other is not None and other.id == twin_id
            # aadha-adhoora naam ab bhi jaan-bujh kar mana karta hai
            assert await task_service.find_staff(db, "Task") is None
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Staff).where(Staff.id == twin_id))
            await db.commit()


async def test_a_number_saved_just_now_is_recognised_immediately(client, sent) -> None:
    """Save karte hi agent us number ko staff maane — koi restart nahi.

    Lookup har message par DB se hota hai, isliye ye turant hona chahiye;
    yeh test us bharose ko pakad kar rakhta hai.
    """
    from tests.conftest import meta_payload, sign_body

    phone_10 = "9999900084"
    phone = "+91" + phone_10
    r = await client.post(
        "/admin/api/staff", headers=H,
        json={"name": "Turantram", "phone": phone_10, "role": "WASHER"},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert "Turantram" in (r.json().get("ready") or "")
    try:
        body = meta_payload(messages=[{
            "from": phone.lstrip("+"), "id": "wamid.TESTturant",
            "type": "text", "text": {"body": "aaj 3 order ho gaye"},
        }])
        resp = await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
        )
        assert resp.status_code == 200
        async with async_session_factory() as db:
            st = await db.get(Staff, uuid.UUID(sid))
            assert st.last_message_at is not None, "staff ki tarah pehchana jana chahiye"
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.wa_message_id == "wamid.TESTturant")
                )
            ).scalar_one()
            assert conv.staff_id == st.id and conv.customer_id is None
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Conversation).where(Conversation.staff_id == uuid.UUID(sid)))
            await db.execute(delete(Staff).where(Staff.id == uuid.UUID(sid)))
            await db.commit()


async def test_deactivated_staff_loses_command_powers(worker, sent, monkeypatch) -> None:
    """Settings mein band karte hi uske purane commands bhi band.

    Pehle deactivate sirf naya kaam rokta tha — wo aadmi WhatsApp se bill
    aur payment abhi bhi likhwa sakta tha.
    """
    called: list[str] = []

    async def fake_staff_message(db, *, sender_phone, sender_label, text):
        called.append(sender_phone)
        return None

    import app.routers.webhook as webhook_module

    monkeypatch.setattr(webhook_module, "handle_staff_message", fake_staff_message)

    async def post(wamid: str) -> None:
        async with async_session_factory() as db:
            await webhook_module._handle_inbound_message(
                {"id": wamid, "from": TASK_STAFF_PHONE.lstrip("+"),
                 "type": "text", "text": {"body": "Sharma ji ka 500 cash aya"}},
                db,
            )

    await post("wamid.TESTactive1")
    assert called == [TASK_STAFF_PHONE], "active staff ke command chalne chahiye"

    async with async_session_factory() as db:
        st = await db.get(Staff, worker)
        st.is_active = False
        await db.commit()

    called.clear()
    await post("wamid.TESTinactive1")
    assert called == [], "band aadmi ka command nahi chalna chahiye"
    async with async_session_factory() as db:
        conv = (
            await db.execute(
                select(Conversation).where(Conversation.wa_message_id == "wamid.TESTinactive1")
            )
        ).scalar_one()
        # message phir bhi unke naam par darj hai — ghost customer nahi banta
        assert conv.staff_id == worker and conv.customer_id is None


# --- roz ke kaam ke message par bhi wahi teen button --------------------


async def test_work_order_to_staff_carries_the_same_buttons(sent, worker) -> None:
    """Naya kaam / "aaj delivery hai" wala reminder — dono par button ho.

    Owner ka zyadatar kaam Ajit aur Ravi se hota hai. Agar reminder plain
    text jaye to unhe likhna padta hai aur jawab kis order ka tha ye
    andaza lagana padta hai. Button ke id mein order number hota hai.
    """
    from app.models import Staff
    from app.services.order_service import create_order
    from app.services.work_orders import send_work_order

    cust = "+919999900083"
    async with async_session_factory() as db:
        st = await db.get(Staff, worker)
        order = await create_order(
            db, customer_phone=cust, customer_name="Kaam Grahak",
            items=[{"type": "Kurta", "qty": 1}], created_by="test",
        )
        order.assigned_washer_id = st.id
        await db.commit()
        number = order.order_number
        sent.clear()
        outcome = await send_work_order(
            db, order, headline="⏰ Aaj delivery hai, abhi ready nahi",
            extra="Pehle ise nipta dein.",
        )
    try:
        assert outcome == "sent"
        card = next(c for c in sent if c["to"] == TASK_STAFF_PHONE)
        assert number in card["text"]
        ids = [b.id for b in (card.get("buttons") or [])]
        assert ids == [f"ord:{number}:done", f"ord:{number}:later", f"ord:{number}:problem"]
    finally:
        from tests.conftest import purge_phones

        await purge_phones(cust)


async def test_task_reminder_repeats_the_status_menu(sent, worker, awake) -> None:
    """Reminder mein bhi wahi clear status menu repeat hota hai."""
    customer_phone = "+919999900086"
    try:
        async with async_session_factory() as db:
            order = await create_order(
                db, customer_phone=customer_phone, customer_name="Reminder customer",
                items=[{"type": "Shirt", "qty": 1}], created_by="test",
            )
            task = await _mk(db, worker, kind="wash", order=order)
            code = task.code
        sent.clear()

        async with async_session_factory() as db:
            st = await db.get(Staff, worker)
            t = (await db.execute(select(Task).where(Task.code == code))).scalar_one()
            assert await task_service._send_to_assignee(db, t, st, first=False)
        ping = next(c for c in sent if c["to"] == TASK_STAFF_PHONE)
        assert "reminder" in ping["text"].lower()
        assert [row.id for row in (ping.get("list_rows") or [])] == [
            f"task:{code}:wash", f"task:{code}:pending",
        ]
    finally:
        await purge_phones(customer_phone)

async def test_operational_team_shares_queue_and_first_completion_wins(worker, sent) -> None:
    """Any active washer can complete a shared task using its explicit button."""
    second_phone = "+919999900089"
    async with async_session_factory() as db:
        second = Staff(
            phone=second_phone, name="Taskram Two", role=StaffRole.WASHER,
            is_active=True, last_message_at=datetime.now(timezone.utc),
        )
        db.add(second)
        await db.commit()
        second_id = second.id
        task = await _mk(db, worker, title="Shared washing queue task", notify=False)
        code = task.code

    try:
        async with async_session_factory() as db:
            visible = await task_service.open_tasks_for_staff(db, second_id)
            assert any(t.code == code for t in visible)

        async with async_session_factory() as db:
            reply = await bill_agent.handle_staff_message(
                db, sender_phone=second_phone, sender_label="Taskram Two",
                text=f"[button:task:{code}:done] Done",
            )
            assert "closed" in (reply or "").lower() or code in (reply or "")

        async with async_session_factory() as db:
            done = await task_service.get_by_code(db, code)
            assert done.status == TASK_DONE
            assert done.reply is None or "done" in (done.reply or "").lower()

        async with async_session_factory() as db:
            duplicate = await bill_agent.handle_staff_message(
                db, sender_phone=TASK_STAFF_PHONE, sender_label="Taskram",
                text=f"[button:task:{code}:done] Done",
            )
            assert "already complete" in (duplicate or "").lower()
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.code == code))
            await db.execute(delete(Staff).where(Staff.id == second_id))
            await db.commit()


async def test_menu_response_is_saved_as_ai_readable_context() -> None:
    from app.services.knowledge import _conversation_for_ai

    assert _conversation_for_ai("[button:bill_confirm:yes] ✅ Haan, Bill Banao").startswith(
        "MENU_RESPONSE: Bill draft -> YES"
    )
    assert "Task T-123 -> YES" in _conversation_for_ai(
        "[button:job_yes:T-123] ✅ Haan, ho gaya"
    )
    assert "Order KK-20260928-01 -> action 'done'" in _conversation_for_ai(
        "[button:ord:KK-20260928-01:done] ✅ Ho gaya"
    )


async def test_waha_display_text_task_status_stays_deterministic(sent) -> None:
    """WAHA list replies without an id must not fall through to the LLM."""
    from app.services import bill_agent

    phone = f"+919999{uuid.uuid4().int % 1_000_000:06d}"
    async with async_session_factory() as db:
        staff = Staff(
            phone=phone,
            name="Delivery Display Test",
            role=StaffRole.DELIVERY,
            is_active=True,
            last_message_at=datetime.now(timezone.utc),
        )
        db.add(staff)
        await db.commit()
        sid = staff.id
        from app.models import Customer
        customer = Customer(
            phone=f"+919999{uuid.uuid4().int % 1_000_000:06d}",
            name="Display Customer", is_active=True,
        )
        db.add(customer)
        await db.commit()
        task = await _mk(
            db,
            sid,
            title="Pickup for Display Test",
            kind="pickup",
            customer=customer,
            notify=False,
        )
        code = task.code

    try:
        async with async_session_factory() as db:
            reply = await bill_agent._handle_task_menu_display_text(
                db,
                phone,
                "Delivery Display Test",
                "Done Pickup or delivery completed",
            )
            assert reply is not None
            updated = await task_service.get_by_code(db, code)
            assert updated.status == TASK_DONE
            assert code in reply or "done" in reply.lower()
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.code == code))
            await db.execute(delete(Staff).where(Staff.id == sid))
            await db.execute(delete(Customer).where(Customer.id == customer.id))
            await db.commit()


async def test_job_task_requires_customer_order(worker) -> None:
    """Pickup/delivery work must always be linked to a customer or order."""
    async with async_session_factory() as db:
        st = await db.get(Staff, worker)
        with pytest.raises(ValueError, match="requires a customer or order"):
            await task_service.create_task(
                db, title="Pickup aur delivery", staff=st,
                kind="pickup", notify=False,
            )


async def test_orphan_job_task_is_closed_instead_of_reminded(worker, sent, awake) -> None:
    """Legacy orphan pickup/delivery tasks must not generate generic reminders."""
    async with async_session_factory() as db:
        task = await task_service.create_task(
            db, title="Legacy orphan pickup", staff=await db.get(Staff, worker),
            kind="general", notify=False,
        )
        task.kind = "pickup"
        task.last_ping_at = datetime.now(timezone.utc) - timedelta(hours=3)
        db.add(task)
        await db.commit()
        code = task.code

    try:
        sent.clear()
        await task_service.run_task_followups()
        async with async_session_factory() as db:
            updated = await task_service.get_by_code(db, code)
            assert updated.status == "CANCELLED"
            assert updated.completed_at is not None
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.code == code))
            await db.commit()


async def test_operational_task_links_customer(worker) -> None:
    """Washing/ironing/etc. can use a customer directly when no order is needed."""
    from app.models import Customer

    async with async_session_factory() as db:
        st = await db.get(Staff, worker)
        customer = Customer(phone=f"+919999{uuid.uuid4().int % 1_000_000:06d}", name="Linked Customer", is_active=True)
        db.add(customer)
        await db.commit()
        task = await task_service.create_task(
            db, title="Wash Linked Customer ke kapde", staff=st,
            customer=customer, kind="wash", notify=False,
        )
        assert task.customer_id == customer.id
        assert task.order_id is None
        await db.delete(task)
        await db.flush()
        await db.delete(customer)
        await db.commit()



async def test_dashboard_can_edit_task_and_persist_priority_and_due_date(client, worker, sent) -> None:
    from app.models import Task
    from sqlalchemy import select

    async with async_session_factory() as db:
        task = await _mk(db, worker, title="Old task")
        code = task.code

    due = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    response = await client.put(f"/admin/api/tasks/{code}", headers=H, json={
        "title": "Updated task instructions",
        "staff": "Taskram",
        "order_number": None,
        "kind": "delivery",
        "due_at": due,
        "priority": "urgent",
        "customer_name": None,
        "customer_phone": None,
    })
    assert response.status_code == 200, response.text

    async with async_session_factory() as db:
        updated = (await db.execute(select(Task).where(Task.code == code))).scalar_one()
        assert updated.title == "Updated task instructions"
        assert updated.kind == "delivery"
        assert updated.urgent is True
        assert updated.due_at is not None
        assert updated.assigned_staff_id == worker



async def test_dashboard_can_reassign_open_task_and_record_owner(client, worker, sent) -> None:
    """Explicit assign endpoint changes the owner without changing task identity."""
    import uuid as _uuid
    from app.models import Task

    second_phone = f"+919999{_uuid.uuid4().int % 1_000_000:06d}"
    async with async_session_factory() as db:
        second = Staff(
            phone=second_phone,
            name="Taskram Reassigned",
            role=StaffRole.WASHER,
            is_active=True,
            last_message_at=datetime.now(timezone.utc),
        )
        db.add(second)
        await db.commit()
        second_id = second.id
        task = await _mk(db, worker, title="Reassignment contract task", notify=False)
        code = task.code

    try:
        response = await client.post(
            f"/admin/api/tasks/{code}/assign",
            headers=H,
            json={"staff": "Taskram Reassigned"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ok"] is True
        assert payload["code"] == code
        assert payload["assigned_staff_id"] == str(second_id)

        async with async_session_factory() as db:
            updated = await task_service.get_by_code(db, code)
            assert updated is not None
            assert updated.assigned_staff_id == second_id
            assert updated.status == TASK_OPEN
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.code == code))
            await db.execute(delete(Staff).where(Staff.id == second_id))
            await db.commit()



async def test_duplicate_open_customer_task_reuses_existing_task(worker, sent) -> None:
    """Repeated automation must not create or notify a second open work item."""
    from app.models import Customer

    phone = f"+919999{uuid.uuid4().int % 1_000_000:06d}"
    async with async_session_factory() as db:
        staff = await db.get(Staff, worker)
        customer = Customer(phone=phone, name="Duplicate Guard Customer", is_active=True)
        db.add(customer)
        await db.commit()
        customer_id = customer.id
        first = await task_service.create_task(
            db, title="Wash this customer's clothes", staff=staff,
            customer=customer, kind="wash", notify=True,
        )
        first_code = first.code

    first_send_count = len(sent)
    try:
        async with async_session_factory() as db:
            staff = await db.get(Staff, worker)
            customer = await db.get(Customer, customer_id)
            second = await task_service.create_task(
                db, title="Duplicate wash request", staff=staff,
                customer=customer, kind="wash", notify=True,
            )
        assert second.code == first_code
        assert len(sent) == first_send_count
        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    select(Task).where(
                        Task.customer_id == customer_id,
                        Task.order_id.is_(None),
                        Task.kind == "wash",
                        Task.status == TASK_OPEN,
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.customer_id == customer_id))
            await db.execute(delete(Customer).where(Customer.id == customer_id))
            await db.commit()


@pytest.mark.parametrize("kind", ["packing", "custom"])
async def test_dashboard_accepts_packing_and_custom_task_kinds(client, kind) -> None:
    """Both task creation and editing must accept every advertised task kind."""
    created_code = None
    try:
        response = await client.post(
            "/admin/api/tasks",
            headers=H,
            json={"title": f"Phase 2 {kind} task", "kind": kind},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["kind"] == kind
        created_code = payload["code"]

        edited = await client.put(
            f"/admin/api/tasks/{created_code}",
            headers=H,
            json={"title": f"Edited Phase 2 {kind} task", "kind": kind},
        )
        assert edited.status_code == 200, edited.text

        async with async_session_factory() as db:
            saved = await task_service.get_by_code(db, created_code)
            assert saved is not None
            assert saved.kind == kind
            assert saved.title == f"Edited Phase 2 {kind} task"
    finally:
        if created_code:
            async with async_session_factory() as db:
                await db.execute(delete(Task).where(Task.code == created_code))
                await db.commit()
