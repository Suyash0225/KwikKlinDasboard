"""AI agent + escalation tests — LLM fully mocked, no key needed."""

import pytest
from sqlalchemy import select, text as sqltext

import app.services.ai_agent as agent_module
import app.services.escalation as escalation_module
from app.config import settings
from app.database import async_session_factory
from app.models import Customer, Escalation
from app.services.ai_agent import build_ai_reply
from app.services.llm_client import LLMUnavailable

PHONE = "+919999900123"


async def _purge() -> None:
    """Remove EVERYTHING attached to the test phone, FK-safe order."""
    async with async_session_factory() as s:
        sub = f"(SELECT id FROM customers WHERE phone = '{PHONE}')"
        await s.execute(
            sqltext(
                "DELETE FROM order_status_history WHERE order_id IN "
                f"(SELECT id FROM orders WHERE customer_id IN {sub})"
            )
        )
        for table in ("escalations", "conversations", "orders"):
            await s.execute(
                sqltext(f"DELETE FROM {table} WHERE customer_id IN {sub}")
            )
        await s.execute(sqltext(f"DELETE FROM customers WHERE phone = '{PHONE}'"))
        await s.commit()


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
    async with async_session_factory() as s:
        cust = Customer(phone=PHONE, name="AI Grahak")
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
    called = False

    async def fake_classify(text):
        nonlocal called
        called = True
        return {"intent": "OTHER", "language": "hi"}

    monkeypatch.setattr(agent_module, "classify_intent", fake_classify)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        assert await build_ai_reply(db, cust, "[image:/admin/media/x.jpg]") is None
        assert await build_ai_reply(db, cust, "") is None
    assert called is False


async def test_classifier_down_returns_none(monkeypatch) -> None:
    async def fake_classify(text):
        return None

    monkeypatch.setattr(agent_module, "classify_intent", fake_classify)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        assert await build_ai_reply(db, cust, "hello ji") is None


async def test_complaint_escalates_and_apologizes(monkeypatch, esc_sent) -> None:
    async def fake_classify(text):
        return {"intent": "COMPLAINT", "language": "hi"}

    monkeypatch.setattr(agent_module, "classify_intent", fake_classify)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        reply = await build_ai_reply(db, cust, "mera kurta kharab ho gaya!")

    assert reply is not None and "manager" in reply
    escs = await _escalations_for(cust.id)
    assert len(escs) == 1 and escs[0].question.startswith("COMPLAINT:")
    # manager + Ravi CC both alerted
    targets = {c["to"] for c in esc_sent}
    assert settings.MANAGER_PHONE in targets
    if settings.ESCALATION_CC_PHONE:
        assert settings.ESCALATION_CC_PHONE in targets
    assert "kharab" in esc_sent[0]["text"]


async def test_compose_happy_path_no_escalation(monkeypatch) -> None:
    async def fake_classify(text):
        return {"intent": "PRICE_QUERY", "language": "hi"}

    async def fake_ask_json(**kw):
        # the FACTS block must carry customer identity, never notes
        assert "AI Grahak" in kw["user_text"]
        assert "notes" not in kw["user_text"].lower()
        return {"reply": "Shirt ₹30 hai ji — Kwik Klin", "escalate": False, "escalation_reason": ""}

    monkeypatch.setattr(agent_module, "classify_intent", fake_classify)
    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        reply = await build_ai_reply(db, cust, "shirt ka kya rate hai")

    assert reply == "Shirt ₹30 hai ji — Kwik Klin"
    assert await _escalations_for(cust.id) == []


async def test_compose_escalate_creates_row(monkeypatch, esc_sent) -> None:
    async def fake_classify(text):
        return {"intent": "NEW_ORDER", "language": "hi"}

    async def fake_ask_json(**kw):
        return {
            "reply": "Manager aapse jald sampark karenge 🙏 — Kwik Klin",
            "escalate": True,
            "escalation_reason": "pickup request",
        }

    monkeypatch.setattr(agent_module, "classify_intent", fake_classify)
    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        reply = await build_ai_reply(db, cust, "kal kapde lene aa jao")

    assert "Manager" in reply
    escs = await _escalations_for(cust.id)
    assert len(escs) == 1 and escs[0].question.startswith("pickup request:")
    assert esc_sent  # alerts attempted


async def test_compose_llm_down_falls_back(monkeypatch) -> None:
    async def fake_classify(text):
        return {"intent": "GREETING", "language": "hi"}

    async def fake_ask_json(**kw):
        raise LLMUnavailable("down")

    monkeypatch.setattr(agent_module, "classify_intent", fake_classify)
    monkeypatch.setattr(agent_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        cust = await _seed_customer()
        assert await build_ai_reply(db, cust, "namaste") is None


async def test_webhook_prefers_ai_reply(client, sent, monkeypatch) -> None:
    """Full inbound flow: AI answer goes out; AI None -> rule-based ack."""
    import app.routers.webhook as webhook_module
    from tests.conftest import meta_payload, sign_body

    async def fake_ai(db, customer, text):
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
    async def fake_ai_none(db, customer, text):
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
    assert "message mil gaya" in sent[-1]["text"]


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
