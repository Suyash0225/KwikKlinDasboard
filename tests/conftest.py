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

# Suite ka apna staff. Pehle yahan asli seeded washer (Ravi) ka number tha,
# isliye owner ke dashboard se number badalte hi 6 tests red ho jaate the.
# Number badalna owner ka haq hai — to ab suite khud apna staff banati hai.
TEST_WASHER_PHONE_RAW = "919999900094"
TEST_WASHER_PHONE = "+919999900094"
TEST_WASHER_NAME = "Qawasher"


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
def _fresh_throttles():
    """Har test saaf throttle-state se shuru ho.

    ASGI test-client ka IP hamesha 'testclient' hota hai — alag-alag files
    ke jaan-boojh kar wale bad-key tests milkar per-IP brute-force window
    (10 fail / 10 min) paar kar dete, aur aage ke sahi-key tests 429 khate
    (jo production mein design hai, suite mein cross-test pollution).
    """
    import app.main as main_mod
    import app.routers.orders as orders_mod
    from app.services import auth as auth_mod

    orders_mod._FAILED_AUTH.clear()
    auth_mod._FAILED.clear()
    main_mod._RL_BUCKETS.clear()
    yield


@pytest.fixture(autouse=True)
async def _prime_home_tenant():
    """Home-tenant cache ko har test se pehle bharo.

    Production mein ye lifespan startup par hota hai; tests ka ASGITransport
    lifespan nahi chalata. Cache ke bina direct-DB inserts NULL-tenant stamp
    hote aur tenant-scoped API reads unhe kabhi nahi dekh paati.
    """
    from app.services import tenant_context

    await tenant_context.get_home_tenant_id()


@pytest.fixture
def real_agent_switch():
    """Ye fixture maangne wale tests par _agent_switch_on patch nahi lagta —
    unhe asli (DB wali) agent_enabled value chahiye."""
    return True


@pytest.fixture(autouse=True)
def _agent_switch_on(monkeypatch, request):
    """Tests LIVE DB par chalte hain aur `agent_enabled` owner ki ASLI
    setting hai — owner ne bot band kiya ho to har auto-reply test jhootha
    fail hota. Read ke waqt True lauta dete hain (DB ko haath nahi lagate,
    warna owner ki setting badal jaati).
    """
    if "real_agent_switch" in request.fixturenames:
        return
    from app.services import app_settings

    real_get = app_settings.get

    async def _get(db, key):
        if key == "agent_enabled":
            return True
        return await real_get(db, key)

    monkeypatch.setattr(app_settings, "get", _get)


@pytest.fixture
async def test_washer():
    """Ek saaf WASHER row — naam/number aise jo owner ke data se kabhi
    na takrayein. Yield karta hai staff id."""
    from sqlalchemy import select

    from app.models import Staff, StaffRole

    async with async_session_factory() as db:
        row = (
            await db.execute(select(Staff).where(Staff.phone == TEST_WASHER_PHONE))
        ).scalar_one_or_none()
        if row is None:
            row = Staff(
                phone=TEST_WASHER_PHONE, name=TEST_WASHER_NAME,
                role=StaffRole.WASHER, is_active=True,
            )
            db.add(row)
            await db.commit()
        sid = row.id
    try:
        yield sid
    finally:
        async with async_session_factory() as db:
            for q in (
                "DELETE FROM conversations WHERE staff_id = :i",
                "UPDATE tasks SET assigned_staff_id = NULL WHERE assigned_staff_id = :i",
                "UPDATE orders SET assigned_washer_id = NULL WHERE assigned_washer_id = :i",
                "UPDATE orders SET assigned_delivery_id = NULL WHERE assigned_delivery_id = :i",
                "DELETE FROM staff WHERE id = :i",
            ):
                await db.execute(sqltext(q), {"i": str(sid)})
            await db.commit()


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

    # Claude ka client ab pehli baar istemaal par banta hai (lazy) — isliye
    # hum client ki jagah BANANE WALE ko patch karte hain. Purana patch
    # module-level `_client` par tha; wo hatte hi ye fixture khud phatne
    # lagi thi aur tests LLM ke naam par kuch aur hi dikhane lage the.
    class _DeadClient:
        class messages:
            create = staticmethod(_anthropic_down)

    monkeypatch.setattr(llm_module, "_anthropic", lambda: _DeadClient)
    monkeypatch.setattr(llm_module, "_anthropic_client", None, raising=False)


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
                f"OR payload::text LIKE '%{TEST_WASHER_PHONE_RAW}%'"
            )
        )
        await s.execute(
            sqltext(
                "DELETE FROM outbound_queue WHERE to_phone IN "
                f"('{TEST_CUSTOMER_PHONE}', '{TEST_WASHER_PHONE}')"
            )
        )
        await s.commit()
    # Test customer ka poora saaf-safai purge_phones se — wo FK ka sahi
    # kram jaanta hai (orders, payments, tasks... phir customer). Pehle
    # yahan seedha "DELETE FROM customers" tha: jis test ne order banaya
    # aur khud saaf nahi kiya, uske baad har agla test isi FK error par
    # gir jaata tha, aur wajah bilkul alag jagah dikhti thi.
    await purge_phones(TEST_CUSTOMER_PHONE)
    async with async_session_factory() as s:
        # Safety net: agar kisi test ne galti se kisi ASLI staff ki 24h
        # window NULL kar di, to Inbox mein uska thread jhooth-much "closed"
        # dikhne lagta hai. Sirf repair karte hain — jahan value gayab hai
        # lekin uska inbound message maujood hai, wahan wapas bhar dete hain.
        # Kisi maujooda value ko chhedte nahi.
        await s.execute(
            sqltext(
                "UPDATE staff s SET last_message_at = i.last_inbound FROM ("
                "  SELECT staff_id, max(created_at) AS last_inbound"
                "  FROM conversations WHERE staff_id IS NOT NULL"
                "    AND direction = 'INBOUND' GROUP BY staff_id"
                ") i WHERE i.staff_id = s.id AND s.last_message_at IS NULL"
            )
        )
        await s.commit()
    await engine.dispose()


# --------------------------------------------------------------------------
# Test-run ka apna kachra: audit_log
# --------------------------------------------------------------------------
# Suite LIVE DB par chalti hai (alag test DB abhi nahi hai). App ka har
# action audit_log mein likhta hai, isliye har run owner ke asli audit
# feed mein 1000+ jhoothi rows chhod jaata tha — panel padhne layak hi
# nahi bachta. Ye fixture SIRF un rows ko hataata hai jo is run ke dauraan
# bani AUR jinke markers pakke test ke hain (real data kabhi nahi chhuta:
# time-window akela kaafi nahi maana, marker bhi match hona chahiye).
_TEST_AUDIT_MARKERS = r"""
     actor ILIKE '%@test.local' OR actor ILIKE '%@example.com'
  OR actor ILIKE 'test-%' OR actor = 'Qatestwala'
  OR args::text ~ '(test-sec-|test-ops-|test-panel-|test-prof-|test-scale-|test-ss-|test-wa-|test-roles-|test-sweep-|test-rl-|test-import|bahar-wali-laundry|trial-test-shop|@test\.local|@example\.com|\+9199999000)'
  OR args::text ILIKE '%TestService%' OR args::text ILIKE '%test-winback%'
  OR args::text ILIKE '%test-pricelist.txt%' OR args::text ILIKE '%Qatestwala%'
"""


@pytest.fixture(scope="session", autouse=True)
def _purge_test_audit_rows():
    """Session ke baad is run ke test-audit rows delete."""
    import asyncio
    from datetime import datetime, timezone

    started = datetime.now(timezone.utc)

    async def purge():
        async with async_session_factory() as db:
            await db.execute(
                sqltext(f"DELETE FROM audit_log WHERE at >= :t AND ({_TEST_AUDIT_MARKERS})"),
                {"t": started},
            )
            await db.commit()

    yield
    try:
        asyncio.run(purge())
    except Exception:
        pass  # cleanup kabhi suite ko fail na kare
