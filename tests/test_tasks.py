"""Assigned tasks: the agent gives work, chases it, and records the answer.

The whole point is that "Ravi se bol do X" stops being a message that
scrolls away and becomes something with an owner and a status.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

import app.services.bill_agent as bill_agent
import app.services.tasks as task_service
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
    assert f"done {task.code}" in body, "they must be told how to close it"


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
    # the owner hears about it without asking
    assert any(c["to"] == settings.MANAGER_PHONE for c in sent)


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

    # just created -> not due yet
    assert await task_service.run_task_followups() == 0

    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
        task.last_ping_at = datetime.now(timezone.utc) - timedelta(hours=3)
        db.add(task)
        await db.commit()

    n = await task_service.run_task_followups()
    assert n == 1
    assert "Reminder" in sent[-1]["text"] and code in sent[-1]["text"]

    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
    assert task.ping_count == 1


async def test_silence_escalates_to_the_manager(worker, sent, awake) -> None:
    async with async_session_factory() as db:
        task = await _mk(db, worker)
        code = task.code
        task.ping_count = task_service.ESCALATE_AFTER_PINGS
        task.last_ping_at = datetime.now(timezone.utc) - timedelta(hours=5)
        db.add(task)
        await db.commit()
    sent.clear()

    await task_service.run_task_followups()
    to_manager = [c for c in sent if c["to"] == settings.MANAGER_PHONE]
    assert to_manager, "the owner must be told when staff go quiet"
    assert code in to_manager[0]["text"]

    async with async_session_factory() as db:
        task = await task_service.get_by_code(db, code)
    assert task.escalated_at is not None

    sent.clear()
    await task_service.run_task_followups()
    assert not [c for c in sent if c["to"] == settings.MANAGER_PHONE], "escalate once, not every tick"


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
