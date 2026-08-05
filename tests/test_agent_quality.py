"""Agent reply-quality rules the owner complained about (04 Aug).

1. "X se pucho ..." must reach X as a DIRECT question, not a copy of the
   owner's imperative ("Pucho kya usne...").
2. Owner asking about a staff member must get the real chat status
   ("jawab nahi diya"), never "dashboard dekh lo".
3. LLM markdown (**bold**, ## heading) must not reach WhatsApp raw.
"""

from datetime import datetime, timedelta, timezone

import pytest

import app.services.bill_agent as bill_agent
from app.database import async_session_factory
from app.models import Conversation, Direction, Staff
from app.services.whatsapp import _wa_format

STAFF_PHONE = "+919999900091"
STAFF_NAME = "Qatestwala"


# the silence note names the staff member, so it scopes cleanly to ours
SILENCE_NOTE = f"({STAFF_NAME} ne iske baad se KOI JAWAB NAHI diya"


async def _purge_qa_staff() -> None:
    """Remove this fixture's rows wherever they came from — a run that died
    mid-test used to leave the row behind and break every later run."""
    from sqlalchemy import delete, select

    from app.models import Task

    async with async_session_factory() as s:
        ids = (
            await s.execute(select(Staff.id).where(Staff.phone == STAFF_PHONE))
        ).scalars().all()
        for sid in ids:
            await s.execute(delete(Task).where(Task.assigned_staff_id == sid))
            await s.execute(delete(Conversation).where(Conversation.staff_id == sid))
        await s.execute(delete(Staff).where(Staff.phone == STAFF_PHONE))
        await s.commit()


@pytest.fixture
async def qa_staff():
    await _purge_qa_staff()
    async with async_session_factory() as s:
        from app.models import StaffRole

        st = Staff(
            phone=STAFF_PHONE, name=STAFF_NAME, role=StaffRole.WASHER,
            is_active=True, last_message_at=datetime.now(timezone.utc),
        )
        s.add(st)
        await s.commit()
        sid = st.id
    yield sid
    await _purge_qa_staff()


# --- 1. relay rewrites imperatives into a direct question ---

async def test_relay_prompt_forbids_copying_imperative() -> None:
    """The extraction prompt must instruct rewriting, with an example."""
    sys_prompt = bill_agent._EXTRACT_SYSTEM
    assert "NEVER copy the sender's imperative" in sys_prompt
    assert "pucho/bolo/bata do" in sys_prompt
    # the worked example keeps the model honest
    assert "Kya aapne Rahul ka pickup kar liya?" in sys_prompt


async def test_relay_sends_rewritten_question(qa_staff, monkeypatch) -> None:
    """Whatever relay_message the model returns is what the staff gets —
    verbatim, with no 'pucho' wrapper added by our code."""
    sent: list[dict] = []

    async def fake_send(db, *, to_phone, text=None, **kw):
        sent.append({"to": to_phone, "text": text})
        return "wamid.TESTQ1"

    # work for staff now goes out through the task tracker, so that is the
    # module whose sender must be stubbed
    import app.services.tasks as tasks_module

    monkeypatch.setattr(bill_agent, "send_message", fake_send)
    monkeypatch.setattr(tasks_module, "send_message", fake_send)
    async with async_session_factory() as db:
        reply = await bill_agent._apply_relay(
            db, "manager",
            {
                "relay_to": STAFF_NAME,
                "relay_message": "Kya aapne Rahul ka pickup kar liya? Update bata dijiye.",
                "staff_name": "",
                "order_number": "",
            },
        )
    assert sent, "relay must actually send"
    body = sent[0]["text"]
    assert "Kya aapne Rahul ka pickup kar liya?" in body
    assert "pucho" not in body.lower()
    assert reply


# --- 2. manager facts expose staff chat status ---

async def test_manager_facts_report_staff_silence(qa_staff) -> None:
    """Our last message with no staff reply after it must be visible in
    FACTS, so the assistant can say 'jawab nahi diya' instead of deflecting."""
    async with async_session_factory() as s:
        s.add(
            Conversation(
                staff_id=qa_staff, direction=Direction.OUTBOUND,
                message_text="manager ki taraf se: Rahul ka pickup hua?",
                wa_message_id="wamid.TESTfacts1", sent_by="bot",
            )
        )
        await s.commit()

    async with async_session_factory() as db:
        facts = await bill_agent._manager_facts(db)

    assert "STAFF CHAT" in facts
    assert STAFF_NAME in facts
    assert SILENCE_NOTE in facts, "silence must be stated explicitly"


async def test_manager_facts_show_staff_reply(qa_staff) -> None:
    async with async_session_factory() as s:
        now = datetime.now(timezone.utc)
        s.add(
            Conversation(
                staff_id=qa_staff, direction=Direction.OUTBOUND,
                message_text="Rahul ka pickup hua?", wa_message_id="wamid.TESTfacts2",
                sent_by="bot", created_at=now - timedelta(minutes=10),
            )
        )
        s.add(
            Conversation(
                staff_id=qa_staff, direction=Direction.INBOUND,
                message_text="Haan ho gaya, 2 baje", wa_message_id="wamid.TESTfacts3",
                created_at=now,
            )
        )
        await s.commit()

    async with async_session_factory() as db:
        facts = await bill_agent._manager_facts(db)

    assert "Haan ho gaya, 2 baje" in facts
    assert SILENCE_NOTE not in facts


async def test_query_prompt_bans_dashboard_deflection() -> None:
    assert "NEVER tell the owner to check the dashboard" in bill_agent._QUERY_SYSTEM
    assert "STAFF CHAT" in bill_agent._QUERY_SYSTEM
    assert "no markdown" in bill_agent._QUERY_SYSTEM


# --- 3. markdown never reaches WhatsApp ---

def test_wa_format_converts_markdown() -> None:
    assert _wa_format("**Pankaj** ka order") == "*Pankaj* ka order"
    assert _wa_format("## Aaj ka summary") == "Aaj ka summary"
    assert _wa_format("- pehla\n- doosra") == "• pehla\n• doosra"
    # multi-line bold (LLMs do this in lists)
    assert "**" not in _wa_format("1. **Pankaj (KK-1):** ruka hua hai")


def test_wa_format_leaves_plain_text_alone() -> None:
    plain = "Aapka order KK-20260803-01 ready hai. Bill ₹450 hai."
    assert _wa_format(plain) == plain
