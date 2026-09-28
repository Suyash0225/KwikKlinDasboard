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
from app.models import TASK_DONE, TASK_OPEN, Conversation, Staff, StaffRole, Task

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


# --- completing ---

async def test_staff_closes_task_with_done_code(worker, sent) -> None:
    async with async_session_factory() as db:
        task = await _mk(db, worker)
        code = task.code

    async with async_session_factory() as db:
        reply = await bill_agent.handle_staff_message(
            db, sender_phone=TASK_STAFF_PHONE, sender_label="Taskram",
            text=f"done {code}",
        )
    assert reply and code in reply

    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
        assert task.status == TASK_DONE
        assert task.completed_at is not None
    # the configured primary admin hears about it without asking
    async with async_session_factory() as db:
        owner_phone = await team.primary_admin_phone(db)
    assert any(c["to"] == owner_phone for c in sent)


async def test_unknown_code_is_not_a_crash(worker, sent) -> None:
    async with async_session_factory() as db:
        reply = await bill_agent.handle_staff_message(
            db, sender_phone=TASK_STAFF_PHONE, sender_label="Taskram", text="done T-9999",
        )
    assert reply and "nahi mila" in reply


async def test_staff_words_are_recorded_on_the_task(worker) -> None:
    async with async_session_factory() as db:
        task = await _mk(db, worker)
        code = task.code
        await task_service.note_reply(db, worker, "machine kharab hai, kal karunga")

    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
    assert task.reply == "machine kharab hai, kal karunga"
    assert task.status == TASK_OPEN, "an excuse is not a completion"


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

async def test_task_status_menu_is_role_specific_and_updates_the_task(client, sent, worker) -> None:
    """Staff gets a list menu instead of typing status commands.

    Washer/supervisor: Wash, Iron, Ready, Pending.
    Delivery: Done, Pending for both pickup and delivery.
    """
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

    phone = "+919999900088"
    async with async_session_factory() as db:
        from app.services.order_service import create_order
        order = await create_order(
            db,
            customer_phone=phone,
            customer_name="Sharma ji",
            created_by="test",
            items=[{"type": "Shirt", "qty": 2}],
        )
        st = await db.get(Staff, worker)
        task = await _mk(
            db, worker, title="Sharma ji ka wash",
            order=order, kind="wash",
        )
        code = task.code

    menu_messages = [c for c in sent if c.get("list_rows")]
    assert menu_messages, "task assignment must contain a list menu"
    rows = menu_messages[-1]["list_rows"]
    ids = [row.id for row in rows]
    assert ids == [
        f"task:{code}:wash",
        f"task:{code}:iron",
        f"task:{code}:ready",
        f"task:{code}:pending",
    ]
    assert [row.title for row in rows] == [
        "🧼 Wash", "👔 Iron", "✅ Ready", "⏳ Pending"
    ]

    sent.clear()
    assert (await post(f"[button:task:{code}:ready] Ready")).status_code == 200
    async with async_session_factory() as db:
        updated = await task_service.get_by_code(db, code)
        order_after = await db.get(type(order), order.id)
    assert order_after.status.name == "READY"
    assert updated.status == TASK_DONE
    assert any(code in (c.get("text") or "") and "Ready" in (c.get("text") or "") for c in sent)

    from tests.conftest import purge_phones
    await purge_phones(phone)


async def test_delivery_task_uses_done_pending_menu(sent) -> None:
    """Pickup and delivery work use the same simple Done/Pending menu."""
    async with async_session_factory() as db:
        worker = Staff(
            phone="+919999900087", name="Delivery Test", role=StaffRole.DELIVERY,
            is_active=True, last_message_at=datetime.now(timezone.utc),
        )
        db.add(worker)
        await db.commit()
        sid = worker.id
        task = await _mk(
            db, sid, title="Rahul ji ka pickup complete karna hai", kind="pickup"
        )
    try:
        menu_messages = [c for c in sent if c.get("list_rows") and c.get("to") == "+919999900087"]
        assert menu_messages
        rows = menu_messages[-1]["list_rows"]
        assert [row.id for row in rows] == [
            f"task:{task.code}:done", f"task:{task.code}:pending"
        ]
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.assigned_staff_id == sid))
            await db.execute(delete(Staff).where(Staff.id == sid))
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
    async with async_session_factory() as db:
        task = await _mk(db, worker)
        code = task.code
    sent.clear()

    async with async_session_factory() as db:
        st = await db.get(Staff, worker)
        t = (await db.execute(select(Task).where(Task.code == code))).scalar_one()
        assert await task_service._send_to_assignee(db, t, st, first=False)
    ping = next(c for c in sent if c["to"] == TASK_STAFF_PHONE)
    assert "reminder" in ping["text"].lower()
    assert [row.id for row in (ping.get("list_rows") or [])] == [
        f"task:{code}:wash", f"task:{code}:iron",
        f"task:{code}:ready", f"task:{code}:pending",
    ]


async def test_operational_team_shares_queue_and_first_completion_wins(worker, sent) -> None:
    """Any active washer can update the shared queue; duplicate completion is harmless."""
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
                text=f"done {code}",
            )
            assert "closed" in (reply or "").lower() or code in (reply or "")

        async with async_session_factory() as db:
            done = await task_service.get_by_code(db, code)
            assert done.status == TASK_DONE
            assert done.reply is None or done.reply == "done"

        async with async_session_factory() as db:
            duplicate = await bill_agent.handle_staff_message(
                db, sender_phone=TASK_STAFF_PHONE, sender_label="Taskram",
                text=f"done {code}",
            )
            assert "already complete" in (duplicate or "").lower()
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Task).where(Task.code == code))
            await db.execute(delete(Staff).where(Staff.id == second_id))
            await db.commit()
