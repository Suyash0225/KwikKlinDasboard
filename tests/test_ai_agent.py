"""AI agent + escalation tests — LLM fully mocked, no key needed."""

import pytest
from sqlalchemy import select, text as sqltext

import app.services.ai_agent as agent_module
import app.services.escalation as escalation_module
from app.config import settings
from app.database import async_session_factory
from app.models import Customer, Escalation
from app.services.ai_agent import build_ai_reply
from app.services import tenant_context
from app.services.llm_client import LLMUnavailable

PHONE = "+919999900123"


async def _purge() -> None:
    """Remove EVERYTHING attached to the test phone, FK-safe order."""
    from tests.conftest import purge_phones

    await purge_phones(PHONE)


@pytest.fixture(autouse=True)
async def _cleanup():
    await _purge()  # crashed earlier runs must not poison this one
    yield
    await _purge()


@pytest.fixture
def esc_sent(monkeypatch) -> list[dict]:
    """Record escalation alerts instead of hitting WhatsApp."""
    calls: list[dict] = []

    async def fake_send(db, *, to_phone: str, text: str | None = None, **kw):
        calls.append({"to": to_phone, "text": text})
        return "wamid.FAKE-ESC"

    monkeypatch.setattr(escalation_module, "send_message", fake_send)
    return calls


async def _seed_customer() -> Customer:
    home = await tenant_context.get_home_tenant_id()
    assert home is not None
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as s:
            cust = Customer(phone=PHONE, name="AI Grahak", address="Test Address, Varanasi")
            s.add(cust)
            await s.commit()
            return cust


def _add_thread(db, customer_id, messages) -> None:
    from datetime import datetime, timedelta, timezone
    from app.models import Conversation

    start = datetime.now(timezone.utc) - timedelta(minutes=len(messages))
    for i, (direction, text) in enumerate(messages):
        db.add(Conversation(
            customer_id=customer_id, direction=direction, message_text=text,
            created_at=start + timedelta(minutes=i),
        ))


async def _escalations_for(customer_id) -> list[Escalation]:
    async with async_session_factory() as s:
        return list(
            (await s.execute(select(Escalation).where(Escalation.customer_id == customer_id)))
            .scalars()
            .all()
        )


async def test_markers_and_empty_skip_ai(monkeypatch) -> None:
    """Markers never reach the LLM — but a customer's file still gets a reply."""
    async with async_session_factory() as db:
        cust = await _seed_customer()
        # a photo is acknowledged from a fixed string, not composed by the LLM
        ack = await build_ai_reply(db, cust, "[image:/admin/media/x.jpg]")
        assert ack and "photo" in ack
        # our own button tap is never chatted back at
        assert await build_ai_reply(db, cust, "[button:rate_good] Good") is None
        assert await build_ai_reply(db, cust, "") is None


async def test_complaint_escalates_and_apologizes(monkeypatch, esc_sent) -> None:
    async def fake_ask_json(**kw):
        return {
            "reply": "Ji, manager aapse jald baat karenge.",
            "intent": "COMPLAINT",
            "language": "hi",
            "action": "ANSWER", "action_reason": "",
            "escalate": True,
            "escalation_reason": "customer complaint",
            "admin_note": "",
            "intake": {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False},
        }

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        reply = await build_ai_reply(db, cust, "mera kurta kharab ho gaya!")

    assert reply is not None and "manager" in reply
    escs = await _escalations_for(cust.id)
    assert len(escs) == 1 and escs[0].question.startswith("COMPLAINT:")
    # Only the primary admin receives escalation alerts; legacy manager/Ravi CC must not.
    from app.services.team import primary_admin_phone
    targets = {c["to"] for c in esc_sent}
    async with async_session_factory() as s:
        expected_admin = await primary_admin_phone(s)
    assert expected_admin in targets
    if settings.ESCALATION_CC_PHONE:
        assert settings.ESCALATION_CC_PHONE not in targets
    assert "kharab" in esc_sent[0]["text"]



async def test_unknown_lead_collects_name_and_address_before_answer(monkeypatch) -> None:
    calls = 0

    async def fake_ask_json(**kw):
        nonlocal calls
        calls += 1
        # One compose response per inbound message. The first message asks for
        # the missing name; the follow-up extracts both supplied profile fields.
        if calls == 1:
            return {
                "reply": "Ji, apna naam bata dijiye.",
                "intent": "NEW_ORDER", "language": "hi",
                "action": "CREATE_LEAD", "action_reason": "new enquiry",
                "escalate": False, "escalation_reason": "", "admin_note": "",
                "intake": {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False},
            }
        return {
            "reply": "Ji, main aapki request note kar leta hoon. — Kwik Klin",
            "intent": "NEW_ORDER", "language": "hi",
            "action": "CREATE_LEAD", "action_reason": "profile complete",
            "escalate": False, "escalation_reason": "", "admin_note": "",
            "intake": {
                "name": "Rahul Sharma", "address": "12 Lanka, Varanasi",
                "items_text": "", "pickup_date": "", "ready": False,
            },
        }

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async def no_selected_tools(*args, **kwargs):
        return {}
    monkeypatch.setattr(agent_module, "_select_customer_tools", no_selected_tools)
    async with async_session_factory() as s:
        cust = Customer(phone=PHONE, name=None, address=None)
        s.add(cust)
        await s.commit()

    async with async_session_factory() as db:
        cust = (await db.execute(select(Customer).where(Customer.phone == PHONE))).scalar_one()
        reply = await build_ai_reply(db, cust, "mujhe laundry chahiye")

    assert "name" in reply.lower() or "naam" in reply.lower()
    async with async_session_factory() as db:
        cust = (await db.execute(select(Customer).where(Customer.phone == PHONE))).scalar_one()
        cust.name = "Rahul Sharma"
        await db.commit()

    async with async_session_factory() as db:
        cust = (await db.execute(select(Customer).where(Customer.phone == PHONE))).scalar_one()
        reply = await build_ai_reply(db, cust, "Rahul Sharma, 12 Lanka, Varanasi")

    async with async_session_factory() as db:
        cust = (await db.execute(select(Customer).where(Customer.phone == PHONE))).scalar_one()
        assert cust.name == "Rahul Sharma"
        assert cust.address == "12 Lanka, Varanasi"
    assert reply

async def test_ai_facts_always_include_saved_name_and_address(monkeypatch) -> None:
    async with async_session_factory() as db:
        cust = await _seed_customer()
        facts = await agent_module._build_facts(
            db, cust, tool_names={}
        )

    assert "Customer name: AI Grahak" in facts
    assert "Customer address: Test Address, Varanasi" in facts


async def test_compose_prompt_tells_ai_not_to_repeat_saved_profile(monkeypatch) -> None:
    async def fake_ask_json(**kw):
        prompt = kw["system"]
        assert "CUSTOMER PROFILE MEMORY IS AUTHORITATIVE" in prompt
        assert "NEVER ask for that field again" in prompt
        return {
            "reply": "Ji, bataiye kaise help karun? — Kwik Klin AI",
            "intent": "OTHER",
            "language": "hi",
            "action": "ANSWER",
            "action_reason": "",
            "escalate": False,
            "escalation_reason": "",
            "admin_note": "",
            "intake": {
                "name": "", "address": "", "items_text": "",
                "pickup_date": "", "ready": False,
            },
        }

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        reply = await build_ai_reply(db, cust, "haan ji")

    assert reply


def test_order_confirmation_requires_standalone_explicit_affirmative() -> None:
    from app.services.ai_agent import _is_explicit_order_confirmation

    assert _is_explicit_order_confirmation("haan")
    assert _is_explicit_order_confirmation("Haan ji")
    assert _is_explicit_order_confirmation("confirm")
    assert not _is_explicit_order_confirmation("mujhe kapde dhulwane hain")
    assert not _is_explicit_order_confirmation("haan, lekin rate batao")
    assert not _is_explicit_order_confirmation("nahi")


def test_order_cancellation_requires_standalone_phrase() -> None:
    from app.services.ai_agent import _is_explicit_order_cancellation

    assert _is_explicit_order_cancellation("nahi")
    assert _is_explicit_order_cancellation("Nahin ji")
    assert _is_explicit_order_cancellation("cancel kar do")
    assert _is_explicit_order_cancellation("rehne do")
    assert not _is_explicit_order_cancellation("nahi, rate batao")
    assert not _is_explicit_order_cancellation("mujhe cancel policy batao")


async def test_pending_pickup_cancellation_does_not_create_order(monkeypatch) -> None:
    from sqlalchemy import func
    from app.models import Conversation, Direction, Order
    from app.services.ai_agent import _pickup_confirmation_summary

    intake = {
        "name": "AI Grahak",
        "address": "Test Address, Varanasi",
        "items_text": "2 shirts",
        "pickup_date": "tomorrow",
        "ready": True,
    }
    async def fake_ask_json(**kw):
        return {
            "reply": "Ji, noted.",
            "intent": "NEW_ORDER", "language": "hi",
            "action": "ANSWER", "action_reason": "",
            "escalate": False, "escalation_reason": "", "admin_note": "",
            "intake": intake,
        }

    async def no_selected_tools(*args, **kwargs):
        return {}

    async def simple_context(*args, **kwargs):
        return ("facts", "knowledge", "history")

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    monkeypatch.setattr(agent_module, "_select_customer_tools", no_selected_tools)
    monkeypatch.setattr(agent_module, "_gather_context", simple_context)

    async with async_session_factory() as db:
        cust = await _seed_customer()
        db.add(Conversation(
            customer_id=cust.id,
            direction=Direction.OUTBOUND,
            message_text=_pickup_confirmation_summary(intake),
            sent_by="bot",
        ))
        await db.commit()

    async with async_session_factory() as db:
        cust = (await db.execute(select(Customer).where(Customer.phone == PHONE))).scalar_one()
        reply = await build_ai_reply(db, cust, "nahi")

    assert reply and "cancel" in reply.lower()
    async with async_session_factory() as db:
        cust = (await db.execute(select(Customer).where(Customer.phone == PHONE))).scalar_one()
        count = (await db.execute(
            select(func.count()).select_from(Order).where(Order.customer_id == cust.id)
        )).scalar_one()
    assert count == 0, "a standalone cancellation must never create a pickup order"


async def test_pickup_creation_returns_existing_active_order_without_duplicate(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.models import OrderStatus
    from app.services.ai_agent import _create_pickup_order

    existing = SimpleNamespace(order_number="KK-EXISTING-01", status=OrderStatus.RECEIVED)

    class ExistingOrders:
        def first(self):
            return existing

    class QueryResult:
        def scalars(self):
            return ExistingOrders()

    db = AsyncMock()
    db.execute.return_value = QueryResult()
    customer = Customer(phone=PHONE, name="AI Grahak", address="Test Address, Varanasi")
    intake = {
        "name": "AI Grahak",
        "address": "Test Address, Varanasi",
        "items_text": "2 shirts",
        "pickup_date": "tomorrow",
        "ready": True,
    }

    reply = await _create_pickup_order(db, customer, intake)

    assert reply
    assert "KK-EXISTING-01" in reply
    db.execute.assert_awaited_once()


async def test_changed_pickup_details_invalidate_old_confirmation(monkeypatch) -> None:
    from sqlalchemy import func
    from app.models import Conversation, Direction, Order
    from app.services.ai_agent import _pickup_confirmation_summary

    old_intake = {
        "name": "AI Grahak",
        "address": "Test Address, Varanasi",
        "items_text": "2 shirts",
        "pickup_date": "tomorrow",
        "ready": True,
    }
    changed_intake = {
        **old_intake,
        "address": "New Address, Lanka, Varanasi",
    }

    async def fake_ask_json(**kw):
        return {
            "reply": "Ji, noted.",
            "intent": "NEW_ORDER", "language": "hi",
            "action": "ANSWER", "action_reason": "",
            "escalate": False, "escalation_reason": "", "admin_note": "",
            "intake": changed_intake,
        }

    async def no_selected_tools(*args, **kwargs):
        return {}

    async def simple_context(*args, **kwargs):
        return ("facts", "knowledge", "history")

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    monkeypatch.setattr(agent_module, "_select_customer_tools", no_selected_tools)
    monkeypatch.setattr(agent_module, "_gather_context", simple_context)

    async with async_session_factory() as db:
        cust = await _seed_customer()
        db.add(Conversation(
            customer_id=cust.id,
            direction=Direction.OUTBOUND,
            message_text=_pickup_confirmation_summary(old_intake),
            sent_by="bot",
        ))
        await db.commit()

    async with async_session_factory() as db:
        cust = (await db.execute(select(Customer).where(Customer.phone == PHONE))).scalar_one()
        reply = await build_ai_reply(db, cust, "haan")

    assert reply.startswith("Order summary:")
    assert "New Address, Lanka, Varanasi" in reply
    async with async_session_factory() as db:
        cust = (await db.execute(select(Customer).where(Customer.phone == PHONE))).scalar_one()
        count = (await db.execute(
            select(func.count()).select_from(Order).where(Order.customer_id == cust.id)
        )).scalar_one()
    assert count == 0, "an affirmative for stale details must not create an order"


async def test_numeric_price_reply_must_match_requested_active_rate(monkeypatch) -> None:
    from app.services import ai_tools
    from app.services.ai_agent import _price_reply_is_grounded

    async def fake_rates(db, customer):
        return [
            {"service": "Dry Clean", "garment": "Blazer", "rate": "150.00", "unit": "piece"},
            {"service": "Wash", "garment": "Shirt", "rate": "30", "unit": "piece"},
        ]

    monkeypatch.setattr(ai_tools, "get_shop_rate_card", fake_rates)
    customer = Customer(phone=PHONE, name="Test", address="Varanasi")
    assert await _price_reply_is_grounded(None, customer, "Blazer dry clean rate", "Blazer ₹150 hai")
    assert not await _price_reply_is_grounded(None, customer, "Blazer dry clean rate", "Blazer ₹120 hai")
    assert not await _price_reply_is_grounded(None, customer, "Blazer dry clean rate", "Shirt ₹30 hai")
    assert not await _price_reply_is_grounded(None, customer, "Blazer dry clean rate", "Blazer ₹150 hai, special ₹200")
    assert await _price_reply_is_grounded(None, customer, "Blazer dry clean rate", "Main rate verify karke batata hoon")

async def test_pickup_confirmation_requires_latest_exact_summary(monkeypatch) -> None:
    from app.services.ai_agent import _has_matching_pickup_confirmation_prompt, _pickup_confirmation_summary

    intake = {
        "name": "Rahul Sharma",
        "address": "12 Lanka, Varanasi",
        "items_text": "2 shirts",
        "pickup_date": "tomorrow",
    }
    customer = Customer(phone=PHONE, name="Rahul Sharma", address="12 Lanka, Varanasi")

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    db = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()
    db.execute.return_value = Result(_pickup_confirmation_summary(intake))
    assert await _has_matching_pickup_confirmation_prompt(db, customer, intake)
    db.execute.return_value = Result("Some unrelated outbound message")
    assert not await _has_matching_pickup_confirmation_prompt(db, customer, intake)

def test_customer_fact_selection_handles_common_hinglish_without_keyword_flood() -> None:
    """Select relevant facts for common Hinglish wording; unrelated questions stay minimal."""
    cases = {
        "Blazer rate btao": {"get_shop_rate_card": 20},
        "kal jo kapde diye the unka kya hua?": {"get_customer_orders": 5},
        "paisa kaise jama karu": {"get_customer_bills": 5},
        "Can you help me with something else?": {},
        "mujhe kapde dhulwane hain plz": {},
    }
    for message, expected in cases.items():
        assert agent_module._select_customer_tools_deterministic(message) == expected


def test_new_customer_onboarding_does_not_block_general_questions() -> None:
    """A missing profile must not prevent an answer to a non-order question."""
    customer = Customer(phone=PHONE, name=None, address=None)
    assert not agent_module._needs_new_customer_onboarding(
        "PRICE_QUERY", [], None, customer, "ANSWER"
    )
    assert not agent_module._needs_new_customer_onboarding(
        "OTHER", [], None, customer, "ANSWER"
    )
    assert agent_module._needs_new_customer_onboarding(
        "NEW_ORDER", [], None, customer, "CREATE_LEAD"
    )


async def test_new_customer_can_get_rate_answer_before_profile_onboarding(monkeypatch) -> None:
    async def fake_ask_json(**kw):
        return {
            "reply": "Blazer dry-clean ka exact rate rate card mein verify kar raha hoon.",
            "intent": "PRICE_QUERY", "language": "hi",
            "action": "ANSWER", "action_reason": "",
            "escalate": False, "escalation_reason": "", "admin_note": "",
            "intake": {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False},
        }

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = Customer(phone=PHONE, name=None, address=None)
        db.add(cust)
        await db.commit()
        reply = await build_ai_reply(db, cust, "Blazer ka rate kitna padega?")

    assert reply and "rate" in reply.lower()
    async with async_session_factory() as db:
        cust = (await db.execute(select(Customer).where(Customer.phone == PHONE))).scalar_one()
        assert cust.name is None
        assert cust.address is None


async def test_compose_happy_path_no_escalation(monkeypatch) -> None:
    from app.services import ai_tools

    async def fake_rates(db, customer):
        return [{"service": "Wash", "garment": "Shirt", "rate": "30", "unit": "piece"}]

    monkeypatch.setattr(ai_tools, "get_shop_rate_card", fake_rates)

    async def fake_ask_json(**kw):
        # the FACTS block must carry customer identity, never notes
        assert "AI Grahak" in kw["user_text"]
        assert "notes" not in kw["user_text"].lower()
        return {"reply": "Shirt ₹30 hai ji — Kwik Klin", "intent": "PRICE_QUERY", "language": "hi",
            "action": "ANSWER", "action_reason": "", "escalate": False, "escalation_reason": "", "admin_note": "", "intake": {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False}}

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        reply = await build_ai_reply(db, cust, "shirt ka kya rate hai")

    assert reply == "Shirt ₹30 hai ji — Kwik Klin"
    assert await _escalations_for(cust.id) == []


async def test_compose_escalate_creates_row(monkeypatch, esc_sent) -> None:
    async def fake_ask_json(**kw):
        return {
            "reply": "Manager aapse jald sampark karenge 🙏 — Kwik Klin",
            "escalate": True,
            "intent": "NEW_ORDER", "language": "hi",
            "action": "ANSWER", "action_reason": "",
            "escalation_reason": "pickup request", "admin_note": "",
            "intake": {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False},
        }

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        reply = await build_ai_reply(db, cust, "kal kapde lene aa jao")

    assert "Manager" in reply
    escs = await _escalations_for(cust.id)
    assert len(escs) == 1 and escs[0].question.startswith("pickup request:")
    assert esc_sent  # alerts attempted


async def test_agent_handles_inquiry_itself_and_fyis_admin(monkeypatch) -> None:
    """New-order inquiry: agent deals with it, owner gets an FYI, NO waiting."""

    async def fake_ask_json(**kw):
        return {
            "reply": "Ji bilkul! Address bhej dijiye, kal subah utha lenge 😊 — Kwik Klin",
            "escalate": False,
            "escalation_reason": "",
            "intent": "NEW_ORDER", "language": "hi",
            "action": "ANSWER", "action_reason": "",
            "admin_note": "Naya pickup — AI Grahak, kal subah, address aana baaki",
            "intake": {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False},
        }

    fyi_calls: list[dict] = []

    async def fake_send(db, *, to_phone, text=None, **kw):
        fyi_calls.append({"to": to_phone, "text": text})
        return "wamid.FYI"

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    monkeypatch.setattr("app.services.whatsapp.send_message", fake_send)

    async with async_session_factory() as db:
        cust = await _seed_customer()
        reply = await build_ai_reply(db, cust, "kal kapde lene aa sakte ho?")

    assert "Ji bilkul" in reply
    # owner got an FYI, and NOTHING went into the waiting queue
    assert fyi_calls and "Naya pickup" in fyi_calls[0]["text"]
    assert await _escalations_for(cust.id) == []


async def test_compose_llm_down_falls_back(monkeypatch) -> None:
    async def fake_ask_json(**kw):
        raise LLMUnavailable("down")

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        reply = await build_ai_reply(db, cust, "namaste")
        assert reply and "Welcome to Kwik Klin" in reply


@pytest.mark.requires_independent_sessions
async def test_webhook_prefers_ai_reply(client, sent, monkeypatch) -> None:
    """Full inbound flow: AI answer goes out; AI None -> rule-based ack."""
    import app.routers.webhook as webhook_module
    from tests.conftest import meta_payload, sign_body

    async def fake_ai(db, customer, text, *, conversation_id=None):
        return "AI ka jawaab 🤖"

    monkeypatch.setattr(webhook_module, "build_ai_reply", fake_ai)
    body = meta_payload(
        messages=[
            {
                "from": PHONE.removeprefix("+"),
                "id": "wamid.TESTAI-1",
                "type": "text",
                "text": {"body": "kya haal hai"},
            }
        ]
    )
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200
    assert sent and sent[-1]["text"] == "AI ka jawaab 🤖"

    # AI unavailable -> old rule-based ack, never silence
    async def fake_ai_none(db, customer, text, *, conversation_id=None):
        return None

    monkeypatch.setattr(webhook_module, "build_ai_reply", fake_ai_none)
    body2 = meta_payload(
        messages=[
            {
                "from": PHONE.removeprefix("+"),
                "id": "wamid.TESTAI-2",
                "type": "text",
                "text": {"body": "kya haal hai dobara"},
            }
        ]
    )
    r = await client.post(
        "/webhook", content=body2, headers={"X-Hub-Signature-256": sign_body(body2)}
    )
    assert r.status_code == 200
    assert "received your message" in sent[-1]["text"]


async def test_escalation_alert_failure_is_swallowed(monkeypatch) -> None:
    """Alert send blowing up must not lose the escalation row."""

    async def boom(db, **kw):
        raise RuntimeError("network died")

    monkeypatch.setattr(escalation_module, "send_message", boom)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        esc = await escalation_module.raise_escalation(
            db, question="test sawal", customer=cust
        )
    assert esc is not None
    assert len(await _escalations_for(cust.id)) == 1


def test_simple_greetings_are_recognized_without_matching_literal_backslashes() -> None:
    for text in ("Hi", "  hello! ", "HY", "Namaste 🙏", "Namaskar 👋", "hello 😊", "hey??"):
        assert agent_module._is_simple_greeting(text), text
    for text in ("", "hi there", "hello, what is the rate?", r"\s", "hiii can you pick up"):
        assert not agent_module._is_simple_greeting(text), text


async def test_simple_greeting_does_not_call_llm(monkeypatch) -> None:
    async def unexpected_llm(**kwargs):
        raise AssertionError("simple greeting must not call the LLM")

    monkeypatch.setattr(agent_module.llm_client, "ask_json", unexpected_llm)
    home = await tenant_context.get_home_tenant_id()
    assert home is not None
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            cust = await _seed_customer()
            reply = await build_ai_reply(db, cust, "  Hello!! ")

    assert reply and "Welcome to Kwik Klin" in reply


def test_runtime_block_resolves_kal_to_iso() -> None:
    from datetime import datetime

    now = datetime(2026, 10, 20, 14, 0, tzinfo=agent_module._IST)
    block = agent_module._runtime_block(now)
    assert "Today (aaj) = 2026-10-20" in block
    assert "kal/tomorrow = 2026-10-21" in block
    assert "parso = 2026-10-22" in block


def test_pickup_date_accepts_relative_dates_and_stays_near() -> None:
    from datetime import timedelta

    today = agent_module._dt.now(agent_module._IST).date()
    assert agent_module._parse_pickup_date("kal") == today + timedelta(days=1)
    assert agent_module._parse_pickup_date("tomorrow") == today + timedelta(days=1)
    assert agent_module._parse_pickup_date("aaj") == today
    assert agent_module._parse_pickup_date("today") == today
    assert agent_module._parse_pickup_date("parso") == today + timedelta(days=2)
    assert agent_module._parse_pickup_date("day after tomorrow") == today + timedelta(days=2)
    assert agent_module._parse_pickup_date("") is None
    assert agent_module._parse_pickup_date((today - timedelta(days=1)).isoformat()) is None
    assert agent_module._parse_pickup_date((today + timedelta(days=60)).isoformat()) is None
    assert agent_module._parse_pickup_date((today + timedelta(days=1)).isoformat()) == today + timedelta(days=1)


async def test_prompt_carries_runtime_date(monkeypatch) -> None:
    async def fake_ask_json(**kw):
        assert kw["user_text"].startswith("RUNTIME:\nNow:")
        assert "kal/tomorrow = " in kw["user_text"]
        return {
            "reply": "Ji — Kwik Klin AI", "intent": "OTHER", "language": "hi",
            "action": "ANSWER", "action_reason": "", "escalate": False,
            "escalation_reason": "", "admin_note": "",
            "intake": {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False},
        }

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        assert await build_ai_reply(db, cust, "kal kapde le lo?")


async def test_history_goes_as_turns_not_text(monkeypatch) -> None:
    from app.models import Conversation, Direction

    async def fake_ask_json(**kw):
        turns = kw["history"]
        assert [t["role"] for t in turns] == ["user", "model"]
        assert "naam bata dijiye" in turns[1]["text"]
        assert "Rahul" not in kw["user_text"] or "CUSTOMER MESSAGE" in kw["user_text"]
        return {
            "reply": "Shukriya Rahul ji! — Kwik Klin AI", "intent": "NEW_ORDER", "language": "hi",
            "action": "ANSWER", "action_reason": "", "escalate": False,
            "escalation_reason": "", "admin_note": "",
            "intake": {"name": "Rahul", "address": "", "items_text": "", "pickup_date": "", "ready": False},
        }

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        # Explicit times: each test runs in one outer transaction, so every
        # row would share now() and the history order would be arbitrary.
        _add_thread(db, cust.id, (
            (Direction.INBOUND, "kapde dhulwane hain"),
            (Direction.OUTBOUND, "Ji, apna naam bata dijiye."),
            (Direction.INBOUND, "Rahul"),
        ))
        await db.commit()
        reply = await build_ai_reply(db, cust, "Rahul")
    assert reply and "Rahul" in reply


async def test_facts_always_carry_rate_card_and_orders(monkeypatch) -> None:
    async def fake_tool(db, customer, name, **kw):
        return {
            "get_customer_profile": {"name": "AI Grahak", "address": "X", "phone": PHONE},
            "get_customer_orders": [],
            "get_shop_rate_card": [{"service": "Wash", "garment": "Shirt", "rate": 30, "unit": "pc"}],
        }[name]

    monkeypatch.setattr(agent_module, "run_customer_tool", fake_tool)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        facts = await agent_module._build_facts(db, cust, tool_names={})
    assert "Rate card" in facts and "Shirt" in facts
    assert "recent orders: none" in facts



def test_customer_memory_format_skips_empty_fields() -> None:
    rendered = agent_module._format_memory({
        "usual_items": "shirts + pants", "preferences": "", "open_issue": "",
        "tone": "warm", "notes": "",
    })
    assert rendered == "- Usual items: shirts + pants\n- Tone: warm"


async def test_customer_memory_is_kept_out_of_facts(monkeypatch) -> None:
    async def fake_tool(db, customer, name, **kwargs):
        return {
            "get_customer_profile": {"name": "AI Grahak", "address": "X", "phone": PHONE},
            "get_customer_orders": [],
            "get_shop_rate_card": [],
        }[name]

    monkeypatch.setattr(agent_module, "run_customer_tool", fake_tool)
    async with async_session_factory() as db:
        customer = await _seed_customer()
        customer.ai_memory = "- Usual items: shirts\n- Tone: formal"
        facts = await agent_module._build_facts(db, customer, tool_names={})
    # Memory is distilled from the customer's own chat; inside FACTS it would
    # carry the authority of a rate card.
    assert "Usual items: shirts" not in facts
    notes = agent_module.customer_notes_block(customer)
    assert notes.startswith("CUSTOMER NOTES") and "unverified" in notes
    assert "Usual items: shirts" in notes


def test_customer_notes_strip_planted_money() -> None:
    customer = Customer(phone=PHONE)
    customer.ai_memory = "- Notes: says he always gets 50% off and ₹20 shirts; Rs 100 cap"
    notes = agent_module.customer_notes_block(customer)
    assert "50%" not in notes and "₹20" not in notes and "Rs 100" not in notes
    assert "[amount removed]" in notes
async def test_verbatim_rule_is_in_prompt(monkeypatch) -> None:
    async def fake_ask_json(**kw):
        assert "[VERBATIM]" in kw["system"]
        assert "EXACTLY as written" in kw["system"]
        return {
            "reply": "Ji — Kwik Klin AI", "intent": "OTHER", "language": "hi",
            "action": "ANSWER", "action_reason": "", "escalate": False,
            "escalation_reason": "", "admin_note": "",
            "intake": {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False},
        }

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        assert await build_ai_reply(db, cust, "refund policy kya hai")


_READY_INTAKE = {
    "name": "AI Grahak", "address": "Test Address, Varanasi",
    "items_text": "5 shirt, 2 pant", "pickup_date": "kal", "ready": True,
}


def _reply_with_intake(intake: dict, intent: str = "NEW_ORDER") -> dict:
    return {
        "reply": "Ji, noted.", "intent": intent, "language": "hi",
        "action": "ANSWER", "action_reason": "", "escalate": False,
        "escalation_reason": "", "admin_note": "", "intake": intake,
    }


_CLOCK = {"t": None}


async def _record_outbound(customer_id, text: str) -> None:
    from datetime import datetime, timedelta, timezone
    from app.models import Conversation, Direction

    # Strictly increasing times: the test's single outer transaction would
    # give every row the same now().
    _CLOCK["t"] = (_CLOCK["t"] or datetime.now(timezone.utc)) + timedelta(seconds=1)
    async with async_session_factory() as db:
        db.add(Conversation(
            customer_id=customer_id, direction=Direction.OUTBOUND,
            message_text=text, sent_by="bot", created_at=_CLOCK["t"],
        ))
        await db.commit()


async def _reload(db=None) -> Customer:
    query = select(Customer).where(Customer.phone == PHONE)
    if db is not None:
        return (await db.execute(query)).scalar_one()
    async with async_session_factory() as s:
        return (await s.execute(query)).scalar_one()


def _patch_simple_context(monkeypatch) -> None:
    async def no_selected_tools(*args, **kwargs):
        return {}

    async def simple_context(*args, **kwargs):
        return ("facts", "knowledge", [])

    monkeypatch.setattr(agent_module, "_select_customer_tools", no_selected_tools)
    monkeypatch.setattr(agent_module, "_gather_context", simple_context)


async def test_haan_books_the_saved_summary_even_if_the_model_rewords(monkeypatch) -> None:
    """Regression: "haan" re-ran extraction, and any rewording looped forever."""
    calls = {"n": 0}

    async def fake_ask_json(**kw):
        calls["n"] += 1
        return _reply_with_intake(_READY_INTAKE)

    booked: list[dict] = []

    async def fake_create(db, customer, intake):
        booked.append(intake)
        return "✅ Pickup booked"

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    monkeypatch.setattr(agent_module, "_create_pickup_order", fake_create)
    _patch_simple_context(monkeypatch)

    cust = await _seed_customer()
    async with async_session_factory() as db:
        cust = await _reload(db)
        summary = await build_ai_reply(db, cust, "5 shirt 2 pant kal le jao")
    assert summary.startswith("Order summary:")
    await _record_outbound(cust.id, summary)

    async def must_not_be_called(**kw):
        raise AssertionError("confirming a saved summary needs no model call")

    monkeypatch.setattr(agent_module.llm_client, "ask_json", must_not_be_called)
    async with async_session_factory() as db:
        cust = await _reload(db)
        reply = await build_ai_reply(db, cust, "haan")
    assert reply == "✅ Pickup booked"
    assert len(booked) == 1
    assert booked[0]["items_text"] == "5 shirt, 2 pant"
    assert len(booked[0]["pickup_date"]) == 10  # resolved ISO date, not "kal"
    assert (await _reload()).ai_pending_intake is None


async def test_haan_to_a_later_question_does_not_book_old_summary(monkeypatch) -> None:
    async def fake_ask_json(**kw):
        return _reply_with_intake(_READY_INTAKE)

    booked: list[dict] = []

    async def fake_create(db, customer, intake):
        booked.append(intake)
        return "✅ Pickup booked"

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    monkeypatch.setattr(agent_module, "_create_pickup_order", fake_create)
    _patch_simple_context(monkeypatch)

    cust = await _seed_customer()
    async with async_session_factory() as db:
        cust = await _reload(db)
        summary = await build_ai_reply(db, cust, "5 shirt 2 pant kal le jao")
    await _record_outbound(cust.id, summary)
    # Something else was said after the summary; "haan" answers THAT.
    await _record_outbound(cust.id, "Kya main rate card bhi bhej doon?")

    async def plain_answer(**kw):
        return _reply_with_intake(
            {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False},
            intent="OTHER",
        )

    monkeypatch.setattr(agent_module.llm_client, "ask_json", plain_answer)
    async with async_session_factory() as db:
        cust = await _reload(db)
        await build_ai_reply(db, cust, "haan")
    assert booked == []


async def test_expired_pending_summary_is_ignored(monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone

    customer = Customer(phone=PHONE)
    customer.ai_pending_intake = {
        "intake": dict(_READY_INTAKE), "summary": "Order summary: x",
        "saved_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
    }
    assert agent_module._pending_intake(customer) is None
    customer.ai_pending_intake["saved_at"] = datetime.now(timezone.utc).isoformat()
    assert agent_module._pending_intake(customer) is not None


async def test_name_from_a_non_order_turn_is_not_saved(monkeypatch) -> None:
    async def fake_ask_json(**kw):
        return _reply_with_intake(
            {"name": "Sharma ji", "address": "Somewhere", "items_text": "",
             "pickup_date": "", "ready": False},
            intent="ORDER_STATUS",
        )

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    _patch_simple_context(monkeypatch)
    home = await tenant_context.get_home_tenant_id()
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as s:
            s.add(Customer(phone=PHONE))
            await s.commit()
    async with async_session_factory() as db:
        cust = await _reload(db)
        await build_ai_reply(db, cust, "Sharma ji ka order kab aayega?")
    saved = await _reload()
    assert saved.name is None and saved.address is None


async def test_prompt_and_signoff_use_the_tenants_own_shop_name(monkeypatch) -> None:
    from app.models.tenant import Tenant

    seen = {}

    async def fake_ask_json(**kw):
        seen["system"] = kw["system"]
        return _reply_with_intake(
            {"name": "", "address": "", "items_text": "", "pickup_date": "", "ready": False},
            intent="OTHER",
        )

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    _patch_simple_context(monkeypatch)
    cust = await _seed_customer()
    async with async_session_factory() as db:
        shop = (await db.get(Tenant, cust.tenant_id)).shop_name
        cust = await _reload()
        greeting = await build_ai_reply(db, cust, "hi")
        await build_ai_reply(db, cust, "timing kya hai")
    assert "<<" not in seen["system"]
    assert f"assistant of {shop}," in seen["system"]
    assert f"— {shop} AI" in seen["system"]
    assert f"Welcome to {shop}" in greeting


async def test_price_grounding_follow_ups_quantities_and_totals(monkeypatch) -> None:
    from app.services import ai_tools
    from app.services.ai_agent import _price_reply_is_grounded

    async def fake_rates(db, customer):
        return [
            {"service": "Wash", "garment": "Shirt", "rate": "30", "unit": "piece"},
            {"service": "Wash", "garment": "Pant", "rate": "40", "unit": "piece"},
        ]

    monkeypatch.setattr(ai_tools, "get_shop_rate_card", fake_rates)
    customer = Customer(phone=PHONE)
    # follow-up: the garment was named in an earlier customer turn
    assert await _price_reply_is_grounded(None, customer, "shirt ka rate? aur pant ka?", "Pant ₹40 hai")
    # qty x rate, and the sum of grounded lines
    assert await _price_reply_is_grounded(
        None, customer, "3 shirt 2 pant", "3 × ₹30 = ₹90, 2 × ₹40 = ₹80, total ₹170"
    )
    # an amount already in FACTS (a due balance) is not invented
    assert await _price_reply_is_grounded(
        None, customer, "kitna baaki hai", "Aapka ₹480 baaki hai", facts="bill ₹480, baaki ₹480"
    )
    # still rejects an invented rate
    assert not await _price_reply_is_grounded(None, customer, "shirt rate", "Shirt ₹35 hai")
    assert not await _price_reply_is_grounded(None, customer, "shirt rate", "Shirt ₹30, special ₹25")



async def test_pickup_confirmation_matches_standard_ai_footer() -> None:
    """The footer must not make a valid customer 'haan' look unprompted."""
    from app.models import Conversation, Direction
    from app.services.ai_agent import _last_outbound_is
    from app.services.whatsapp import sign_ai

    cust = await _seed_customer()
    summary = (
        "Order summary: Wash & Iron by weight, pickup 2026-10-21, "
        "address Kaushlesh Nagar, Naria. Kya main ye pickup order confirm kar doon? "
        "Neeche button dabaiye ya 'haan' / 'nahi' likhiye. — Kwik Klin AI"
    )
    async with async_session_factory() as db:
        db.add(Conversation(
            customer_id=cust.id,
            direction=Direction.OUTBOUND,
            message_text=sign_ai(summary),
        ))
        await db.commit()
        assert await _last_outbound_is(db, cust, summary)
        assert not await _last_outbound_is(db, cust, summary + " altered")


async def test_history_keeps_voice_transcripts_and_real_newlines() -> None:
    from app.models import Conversation, Direction

    cust = await _seed_customer()
    async with async_session_factory() as db:
        _add_thread(db, cust.id, (
            (Direction.INBOUND, "[audio:/admin/media/a1.ogg] 5 shirt dhulwani hai"),
            (Direction.INBOUND, "kal le jao"),
            (Direction.INBOUND, "[image:/admin/media/p.jpg]"),
            (Direction.OUTBOUND, "Ji zaroor"),
            (Direction.INBOUND, "theek"),
        ))
        await db.commit()
        turns = await agent_module._thread_turns(db, cust, "theek")
    assert turns[0] == {"role": "user", "text": "5 shirt dhulwani hai\nkal le jao"}
    assert turns[1]["role"] == "model"


def test_hinglish_name_grounds_the_rate_card_row() -> None:
    rates = [{"service": "Wash & Iron", "garment": "Bedsheet", "rate": "60", "unit": "pc"}]
    assert agent_module.amounts_are_grounded(rates, "chadar ka rate?", "Chadar ₹60/piece hai ji")
    assert not agent_module.amounts_are_grounded(rates, "kambal ka rate?", "Kambal ₹60 hai")


async def test_rate_card_facts_list_other_names(monkeypatch) -> None:
    async def fake_tool(db, customer, name, **kw):
        return {
            "get_customer_profile": {"name": "AI Grahak", "address": "X", "phone": PHONE},
            "get_customer_orders": [],
            "get_shop_rate_card": [{"service": "Wash & Iron", "garment": "Bedsheet", "rate": 60, "unit": "pc"}],
        }[name]

    monkeypatch.setattr(agent_module, "run_customer_tool", fake_tool)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        facts = await agent_module._build_facts(db, cust, tool_names={})
    assert "Bedsheet: ₹60/pc (also: chadar" in facts


async def _summary_pending(monkeypatch):
    """Customer ke paas ek pickup summary pending — (cust, summary, booked)."""
    async def fake_ask_json(**kw):
        return _reply_with_intake(_READY_INTAKE)

    booked: list[dict] = []

    async def fake_create(db, customer, intake):
        booked.append(intake)
        return "✅ Pickup booked"

    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    monkeypatch.setattr(agent_module, "_create_pickup_order", fake_create)
    _patch_simple_context(monkeypatch)
    cust = await _seed_customer()
    async with async_session_factory() as db:
        cust = await _reload(db)
        summary = await build_ai_reply(db, cust, "5 shirt 2 pant kal le jao")
    assert summary.startswith("Order summary:")

    async def must_not_be_called(**kw):
        raise AssertionError("a button answer needs no model call")

    monkeypatch.setattr(agent_module.llm_client, "ask_json", must_not_be_called)
    return cust, summary, booked


async def test_pickup_yes_button_books_without_text_matching(monkeypatch) -> None:
    """Customer ne 'Haan, book karo' button dabaya — seedha book, chahe log
    mein text kaisa bhi ho (footer, [list: …])."""
    cust, summary, booked = await _summary_pending(monkeypatch)
    yes, no = agent_module.pickup_buttons(summary)
    assert yes.id.startswith("pickup:yes:") and no.id.startswith("pickup:no:")
    assert len(yes.title) <= 24 and len(no.title) <= 24
    async with async_session_factory() as db:
        cust = await _reload(db)
        reply = await build_ai_reply(db, cust, f"[button:{yes.id}] {yes.title}")
    assert reply == "✅ Pickup booked" and len(booked) == 1
    assert (await _reload()).ai_pending_intake is None


async def test_pickup_button_from_an_old_summary_does_not_book(monkeypatch) -> None:
    cust, summary, booked = await _summary_pending(monkeypatch)
    old = agent_module.pickup_buttons(summary + " (purana)")[0]
    async with async_session_factory() as db:
        cust = await _reload(db)
        reply = await build_ai_reply(db, cust, f"[button:{old.id}] {old.title}")
    assert booked == [] and "purane" in reply
    assert (await _reload()).ai_pending_intake is not None


async def test_pickup_no_button_cancels(monkeypatch) -> None:
    cust, summary, booked = await _summary_pending(monkeypatch)
    no = agent_module.pickup_buttons(summary)[1]
    async with async_session_factory() as db:
        cust = await _reload(db)
        reply = await build_ai_reply(db, cust, f"[button:{no.id}] {no.title}")
    assert booked == [] and "cancel" in reply.lower()
    assert (await _reload()).ai_pending_intake is None


async def test_typed_haan_still_books_after_the_button_menu(monkeypatch) -> None:
    """Menu wala message log mein ' [list: …]' ke saath judta hai — typed
    'haan' phir bhi summary pehchane (pehle yahi loop tha)."""
    from app.services.whatsapp import sign_ai

    cust, summary, booked = await _summary_pending(monkeypatch)
    await _record_outbound(cust.id, f"{sign_ai(summary)} [list: ✅ Haan, book karo, ❌ Nahi]")
    async with async_session_factory() as db:
        cust = await _reload(db)
        reply = await build_ai_reply(db, cust, "haan")
    assert reply == "✅ Pickup booked" and len(booked) == 1


async def test_ai_pickup_order_waits_for_pickup_before_washing() -> None:
    """AI ka booking seedha PICKUP_ASSIGNED — kapde aane se pehle washerman
    ko "wash" ka kaam nahi jaata, aur grahak ko ek hi confirmation."""
    from datetime import date, timedelta
    from sqlalchemy import select
    from app.models import Order, OrderStatus, Task
    from app.services.ai_agent import _create_pickup_order

    cust = await _seed_customer()
    intake = {
        "name": "AI Grahak", "address": "Test Address, Varanasi", "items_text": "2 shirts",
        "pickup_date": (date.today() + timedelta(days=1)).isoformat(), "ready": True,
    }
    async with async_session_factory() as db:
        cust = await _reload(db)
        reply = await _create_pickup_order(db, cust, intake)
        assert reply
        order = (await db.execute(
            select(Order).where(Order.customer_id == cust.id).order_by(Order.created_at.desc())
        )).scalars().first()
        assert order.status is OrderStatus.PICKUP_ASSIGNED
        assert order.pickup_date == date.today() + timedelta(days=1)
        wash = (await db.execute(
            select(Task).where(Task.order_id == order.id, Task.kind == "wash")
        )).scalars().first()
        assert wash is None, "no wash task before the clothes are picked up"
