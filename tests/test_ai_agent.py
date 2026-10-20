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


def test_pickup_date_must_be_iso_and_near() -> None:
    from datetime import timedelta

    today = agent_module._dt.now(agent_module._IST).date()
    assert agent_module._parse_pickup_date("kal") is None
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
        db.add_all([
            Conversation(customer_id=cust.id, direction=Direction.INBOUND, message_text="kapde dhulwane hain"),
            Conversation(customer_id=cust.id, direction=Direction.OUTBOUND, message_text="Ji, apna naam bata dijiye."),
            Conversation(customer_id=cust.id, direction=Direction.INBOUND, message_text="Rahul"),
        ])
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


async def test_customer_memory_is_in_customer_facts(monkeypatch) -> None:
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
    assert "already know about this customer" in facts
    assert "Usual items: shirts" in facts
