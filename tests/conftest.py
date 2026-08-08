"""Shared fixtures for the test suite.

These tests run against the LOCAL dev database (same DATABASE_URL as the
app). Every test cleans up after itself: test rows use recognizable markers
(wamid.TEST*, phone +919999900011) and are deleted in the autouse fixture.

The WhatsApp send function is monkeypatched in webhook tests — tests must
never call Meta's real API.
"""

import hashlib
import hmac
import json

import anthropic
import httpx
import pytest
from sqlalchemy import text as sqltext

import app.routers.webhook as webhook_module
import app.services.llm_client as llm_module
import app.services.order_service as order_service_module
from app.config import settings
from app.database import async_session_factory, engine
from app.main import app

TEST_CUSTOMER_PHONE_RAW = "919999900011"
TEST_CUSTOMER_PHONE = "+919999900011"
RAVI_PHONE_RAW = "918707093136"
RAVI_PHONE = "+918707093136"


async def purge_phones(*phones: str) -> None:
    """Delete EVERYTHING attached to these customer phones, FK-safe order.

    One place to maintain — when a new table references customers/orders,
    add it here and every test file's cleanup is fixed at once.
    """
    if not phones:
        return
    in_list = ", ".join(f"'{p}'" for p in phones)
    sub = f"(SELECT id FROM customers WHERE phone IN ({in_list}))"
    orders_sub = f"(SELECT id FROM orders WHERE customer_id IN {sub})"
    async with async_session_factory() as s:
        for stmt in (
            f"DELETE FROM payments WHERE order_id IN {orders_sub}",
            f"DELETE FROM order_status_history WHERE order_id IN {orders_sub}",
            f"DELETE FROM coupon_redemptions WHERE customer_id IN {sub}",
            f"DELETE FROM campaign_recipients WHERE customer_id IN {sub}",
            f"DELETE FROM open_questions WHERE customer_id IN {sub}",
            # pickup/delivery tasks point at orders — they must go first
            f"DELETE FROM tasks WHERE order_id IN {orders_sub}",
            f"DELETE FROM escalations WHERE customer_id IN {sub}",
            f"DELETE FROM conversations WHERE customer_id IN {sub}",
            f"DELETE FROM orders WHERE customer_id IN {sub}",
            f"DELETE FROM customers WHERE phone IN ({in_list})",
        ):
            await s.execute(sqltext(stmt))
        await s.commit()


def sign_body(body: bytes) -> str:
    """Compute the X-Hub-Signature-256 header exactly like Meta does."""
    digest = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def meta_payload(
    messages: list | None = None,
    statuses: list | None = None,
    contacts: list | None = None,
) -> bytes:
    """Build a Meta webhook body in their entry/changes/value shape.

    contacts carries the sender's WhatsApp profile name, exactly as Meta
    sends it: [{"wa_id": "9199...", "profile": {"name": "Sharma Ji"}}].
    """
    value: dict = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": settings.WHATSAPP_PHONE_NUMBER_ID},
    }
    if messages:
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses
    if contacts:
        value["contacts"] = contacts
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [{"id": "ent1", "changes": [{"field": "messages", "value": value}]}],
        }
    ).encode()


@pytest.fixture(autouse=True)
def _no_live_llm(monkeypatch):
    """No test may reach a real LLM — block at the HTTP/SDK boundary.

    Everything above (ask_json, classify_intent, ...) runs for real and sees
    a 'network outage', so the degrade paths behave exactly like production
    without a connection. Tests that want LLM behavior patch a higher layer
    (ask_json / build_ai_reply / _gemini_post) and their patch wins.
    """

    async def _gemini_down(model, payload):
        raise httpx.ConnectError("live LLM blocked in tests")

    monkeypatch.setattr(llm_module, "_gemini_post", _gemini_down)

    async def _anthropic_down(**kwargs):
        raise anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )

    monkeypatch.setattr(llm_module._client.messages, "create", _anthropic_down)


@pytest.fixture
async def client():
    """HTTP client wired straight into the FastAPI app (no network)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    """Replace the real WhatsApp send with a recorder. Returns the call list.

    Patches BOTH import sites (webhook replies and order_service
    notifications) — tests must never hit Meta's real API.
    """
    calls: list[dict] = []

    async def fake_send(db, *, to_phone: str, text: str | None = None, **kwargs):
        calls.append({"to": to_phone, "text": text, **kwargs})
        return "wamid.FAKE"

    import app.services.bill_agent as bill_agent_module
    import app.services.escalation as escalation_module
    import app.services.tasks as tasks_module
    import app.services.team as team_module
    import app.services.whatsapp as whatsapp_module
    import app.services.work_orders as work_orders_module

    monkeypatch.setattr(webhook_module, "send_message", fake_send)
    monkeypatch.setattr(order_service_module, "send_message", fake_send)
    monkeypatch.setattr(work_orders_module, "send_message", fake_send)
    monkeypatch.setattr(escalation_module, "send_message", fake_send)
    monkeypatch.setattr(bill_agent_module, "send_message", fake_send)
    monkeypatch.setattr(tasks_module, "send_message", fake_send)
    # every "owner ko bata do" goes through team.notify_admins
    monkeypatch.setattr(team_module, "send_message", fake_send)
    del whatsapp_module  # the real door stays intact — see _no_live_whatsapp
    return calls


@pytest.fixture(autouse=True)
def _no_live_whatsapp(monkeypatch):
    """No test may reach Meta — block the HTTP door, not send_message itself.

    Patching send_message would hide the window checks and the conversation
    recording that several tests exist to verify. Blocking one layer lower
    keeps all of that running while making a real API call impossible.
    Tests that patch _post_with_retry themselves still win (applied later).
    """
    import uuid as _uuid

    import app.services.whatsapp as whatsapp_module

    # Meta hands out a UNIQUE id per message; returning a constant made the
    # second send collide on uq_conversations_wa_message_id. Unique across
    # the whole run, not just one test — rows outlive the test that made them.
    async def _blocked(payload, to_phone):
        return {"messages": [{"id": f"wamid.TESTBLOCKED{_uuid.uuid4().hex[:12]}"}]}

    monkeypatch.setattr(whatsapp_module, "_post_with_retry", _blocked)


@pytest.fixture(autouse=True)
async def _cleanup_test_rows():
    """Remove anything a test created; reset Ravi's window; free the pool.

    engine.dispose() at the end matters: pytest-asyncio gives each test its
    own event loop, and pooled asyncpg connections are loop-bound.
    """
    from datetime import datetime, timezone

    started = datetime.now(timezone.utc)
    yield
    async with async_session_factory() as s:
        # Stubbed LLM calls still reach the usage recorder — without this the
        # owner's AI-cost dashboard fills up with numbers from the test suite.
        await s.execute(
            sqltext("DELETE FROM llm_usage WHERE at >= :t"), {"t": started}
        )
        await s.execute(
            sqltext("DELETE FROM conversations WHERE wa_message_id LIKE 'wamid.TEST%'")
        )
        # webhook journal + outbound queue rows from test payloads
        await s.execute(
            sqltext(
                "DELETE FROM webhook_events WHERE payload::text LIKE '%wamid.TEST%' "
                f"OR payload::text LIKE '%{TEST_CUSTOMER_PHONE_RAW}%' "
                f"OR payload::text LIKE '%{RAVI_PHONE_RAW}%'"
            )
        )
        await s.execute(
            sqltext(
                "DELETE FROM outbound_queue WHERE to_phone IN "
                f"('{TEST_CUSTOMER_PHONE}', '{RAVI_PHONE}')"
            )
        )
        # live-LLM accidents may have raised escalations on the test customer
        await s.execute(
            sqltext(
                "DELETE FROM escalations WHERE customer_id IN "
                f"(SELECT id FROM customers WHERE phone = '{TEST_CUSTOMER_PHONE}')"
            )
        )
        # any conversation attached to the test customer (whatever its wamid)
        # must go before the customer row — FK order
        await s.execute(
            sqltext(
                "DELETE FROM conversations WHERE customer_id IN "
                f"(SELECT id FROM customers WHERE phone = '{TEST_CUSTOMER_PHONE}')"
            )
        )
        await s.execute(
            sqltext(f"DELETE FROM customers WHERE phone = '{TEST_CUSTOMER_PHONE}'")
        )
        await s.execute(
            sqltext(f"UPDATE staff SET last_message_at = NULL WHERE phone = '{RAVI_PHONE}'")
        )
        await s.commit()
    await engine.dispose()
