"""The agent's lookup tools + the tool-use loop.

These are what let the owner ask ANY question ("Pankaj ka kya hua",
"kaun late hai", "kitna kharcha") instead of only the ones we hard-coded
into a facts dump.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import app.services.agent_tools as at
import app.services.bill_agent as ba
from app.database import async_session_factory
from app.models import (
    Conversation,
    Customer,
    Direction,
    Order,
    OrderStatus,
    PaymentMethod,
    Staff,
    StaffRole,
)
from app.services import order_service
from tests.conftest import TEST_CUSTOMER_PHONE, purge_phones

TOOL_STAFF_PHONE = "+919999900092"


@pytest.fixture
async def sample_order(sent):
    """A real order with a payment, through the real service layer."""
    async with async_session_factory() as db:
        order = await order_service.create_order(
            db,
            customer_phone=TEST_CUSTOMER_PHONE,
            customer_name="Toolwala Grahak",
            items=[{"type": "shirt", "qty": 2, "service": "wash_iron"}],
            total_amount=Decimal("400"),
            created_by="test",
        )
        await order_service.record_payment(
            db, order, amount=Decimal("150"), method=PaymentMethod.CASH
        )
        num = order.order_number
    yield num
    await purge_phones(TEST_CUSTOMER_PHONE)


async def test_order_detail_reports_money_and_status(sample_order) -> None:
    async with async_session_factory() as db:
        out = await at.run_tool(db, "order_detail", sample_order)
    assert sample_order in out
    assert "Toolwala Grahak" in out
    assert "₹400" in out and "₹150" in out
    assert "₹250" in out, "baaki amount must be spelled out"
    assert "PARTIAL" in out


async def test_customer_detail_lists_orders(sample_order) -> None:
    async with async_session_factory() as db:
        out = await at.run_tool(db, "customer_detail", "Toolwala")
    assert TEST_CUSTOMER_PHONE in out
    assert sample_order in out
    assert "baaki ₹250" in out


async def test_customer_detail_unknown_is_graceful() -> None:
    async with async_session_factory() as db:
        out = await at.run_tool(db, "customer_detail", "KoiAisaAadmiNahiHai")
    assert "nahi mila" in out


async def test_search_orders_filters(sample_order) -> None:
    async with async_session_factory() as db:
        active = await at.run_tool(db, "search_orders", "active")
        unpaid = await at.run_tool(db, "search_orders", "unpaid")
        bad = await at.run_tool(db, "search_orders", "status:NOTAREALSTATUS")
    assert sample_order in active
    assert sample_order in unpaid
    assert "koi status nahi" in bad


async def test_unknown_tool_never_raises() -> None:
    async with async_session_factory() as db:
        out = await at.run_tool(db, "make_me_chai", "now")
    assert "nahi hai" in out


@pytest.fixture
async def tool_staff():
    async with async_session_factory() as s:
        st = Staff(
            phone=TOOL_STAFF_PHONE, name="Toolstaff", role=StaffRole.WASHER,
            is_active=True, last_message_at=datetime.now(timezone.utc),
        )
        s.add(st)
        await s.commit()
        sid = st.id
    yield sid
    async with async_session_factory() as s:
        from sqlalchemy import delete

        await s.execute(delete(Conversation).where(Conversation.staff_id == sid))
        await s.execute(delete(Staff).where(Staff.id == sid))
        await s.commit()


async def test_staff_chat_flags_no_reply(tool_staff) -> None:
    async with async_session_factory() as s:
        s.add(
            Conversation(
                staff_id=tool_staff, direction=Direction.OUTBOUND,
                message_text="Pickup hua?", wa_message_id="wamid.TESTtool1", sent_by="bot",
                created_at=datetime.now(timezone.utc) - timedelta(hours=3),
            )
        )
        await s.commit()
    async with async_session_factory() as db:
        out = await at.run_tool(db, "staff_chat", "Toolstaff")
    assert "JAWAB NAHI diya" in out
    assert "3 ghante" in out


async def test_ping_staff_sends(tool_staff, monkeypatch) -> None:
    sent: list[dict] = []

    async def fake_send(db, *, to_phone, text=None, **kw):
        sent.append({"to": to_phone, "text": text})
        return "wamid.TESTping"

    import app.services.whatsapp as wa

    monkeypatch.setattr(wa, "send_message", fake_send)
    async with async_session_factory() as db:
        out = await at.run_tool(db, "ping_staff", "Toolstaff | Kal subah 9 baje aa jana")
    assert sent and sent[0]["to"] == TOOL_STAFF_PHONE
    assert "Kal subah 9 baje aa jana" in sent[0]["text"]
    assert "bhej diya" in out


async def test_money_tool_reports_period() -> None:
    async with async_session_factory() as db:
        out = await at.run_tool(db, "money", "month")
    assert "Naye order" in out and "Paisa aaya" in out and "Kharcha" in out


# --- the loop itself (LLM stubbed, so this tests OUR orchestration) ---

async def test_loop_runs_tool_then_answers(monkeypatch) -> None:
    steps = [
        {"tool": "order_detail", "args": "KK-1", "answer": ""},
        {"tool": "", "args": "", "answer": "Order ready hai, ₹250 baaki."},
    ]
    seen: list[str] = []

    async def fake_ask_json(*, system, user_text, schema, model=None, max_tokens=0):
        seen.append(user_text)
        return steps.pop(0)

    async def fake_tool(db, name, args):
        return "TOOL-OUTPUT: status READY, baaki 250"

    monkeypatch.setattr(ba.llm_client, "ask_json", fake_ask_json)
    monkeypatch.setattr(at, "run_tool", fake_tool)

    async with async_session_factory() as db:
        ans = await ba._answer_manager_query(db, "KK-1 ka kya hua")

    assert ans == "Order ready hai, ₹250 baaki."
    assert "TOOL-OUTPUT" in seen[-1], "tool result must be fed back to the model"


async def test_loop_stops_after_max_rounds(monkeypatch) -> None:
    """A model that keeps calling tools must still produce an answer."""
    calls = {"n": 0}

    async def always_tool(*, system, user_text, schema, model=None, max_tokens=0):
        calls["n"] += 1
        return {"tool": "search_orders", "args": "active", "answer": ""}

    async def fake_ask(*, system, user_text, model=None, max_tokens=0):
        return "Jo mila usse: 2 order active hain."

    async def fake_tool(db, name, args):
        return "2 active"

    monkeypatch.setattr(ba.llm_client, "ask_json", always_tool)
    monkeypatch.setattr(ba.llm_client, "ask", fake_ask)
    monkeypatch.setattr(at, "run_tool", fake_tool)

    async with async_session_factory() as db:
        ans = await ba._answer_manager_query(db, "kya chal raha hai")

    assert calls["n"] == ba.MAX_TOOL_ROUNDS, "loop must be bounded"
    assert "2 order active" in ans
