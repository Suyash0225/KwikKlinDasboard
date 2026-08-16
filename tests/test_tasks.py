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
    assert any(code in (c["text"] or "") and "Reminder" in (c["text"] or "") for c in sent)

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


# ---------------------------------------------------- task buttons -------

async def test_task_buttons_close_ask_eta_and_report_problem(client, sent, worker) -> None:
    """Ravi/Ajit ko sirf tap karna ho — likhna majboori na ho.

    Teen button: ✅ Ho gaya (task band), ⏳ Time lagega (ETA poochho aur
    owner ko batao), ❓ Dikkat hai (owner ko turant khabar). Button id mein
    task ka CODE hota hai, isliye 5-6 kaam ek saath hone par bhi kabhi
    galat task band nahi hota.
    """
    import uuid as _uuid

    from tests.conftest import meta_payload, sign_body

    async def post(text):
        tag = _uuid.uuid4().hex[:8]
        body = meta_payload(messages=[{
            "from": TASK_STAFF_PHONE.lstrip("+"), "id": f"wamid.TB-{tag}",
            "type": "text", "text": {"body": text},
        }])
        return await client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
        )

    async with async_session_factory() as db:
        t1 = await _mk(db, worker, title="Anmol ka press")
        t2 = await _mk(db, worker, title="Sharma ji ka wash")

    # message ke saath buttons gaye — typing majboori nahi
    with_btns = [c for c in sent if c.get("buttons")]
    assert with_btns, "task message par buttons jane chahiye"
    ids = [b.id for b in with_btns[-1]["buttons"]]
    assert f"task:{t2.code}:done" in ids and f"task:{t2.code}:later" in ids

    # t2 par "Ho gaya" -> sirf t2 band, t1 chhua na jaye
    sent.clear()
    assert (await post(f"[button:task:{t2.code}:done] Ho gaya")).status_code == 200
    async with async_session_factory() as db:
        a = (await db.execute(select(Task).where(Task.code == t2.code))).scalar_one()
        b = (await db.execute(select(Task).where(Task.code == t1.code))).scalar_one()
    assert a.status == TASK_DONE, "jo button dabaya wahi band hona chahiye"
    assert b.status == TASK_OPEN, "doosra task galti se band nahi hona chahiye"

    # t1 par "Time lagega" -> ETA poochha jaye, agla free-text ETA ban jaye
    sent.clear()
    assert (await post(f"[button:task:{t1.code}:later] Time lagega")).status_code == 200
    assert any("kab tak" in (c.get("text") or "").lower() for c in sent)

    sent.clear()
    assert (await post("sham tak ho jayega")).status_code == 200
    async with async_session_factory() as db:
        b = (await db.execute(select(Task).where(Task.code == t1.code))).scalar_one()
    assert b.eta_text == "sham tak ho jayega", "ETA task par save hona chahiye"
    assert any("sham tak" in (c.get("text") or "") for c in sent), "owner ko ETA jaana chahiye"

    # "Dikkat hai" -> owner ko khabar + staff se wajah
    sent.clear()
    assert (await post(f"[button:task:{t1.code}:problem] Dikkat hai")).status_code == 200
    assert any("dikkat" in (c.get("text") or "").lower() for c in sent)


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


async def test_task_reminder_repeats_the_buttons(sent, worker, awake, monkeypatch) -> None:
    """Yaad-dahani par bhi button — pehle message par hi nahi."""
    async with async_session_factory() as db:
        task = await _mk(db, worker)
        code = task.code
    sent.clear()

    async with async_session_factory() as db:
        st = await db.get(Staff, worker)
        t = (await db.execute(select(Task).where(Task.code == code))).scalar_one()
        assert await task_service._send_to_assignee(db, t, st, first=False)
    ping = next(c for c in sent if c["to"] == TASK_STAFF_PHONE)
    assert "Reminder" in ping["text"]
    assert [b.id for b in (ping.get("buttons") or [])] == [
        f"task:{code}:done", f"task:{code}:later", f"task:{code}:problem",
    ]
