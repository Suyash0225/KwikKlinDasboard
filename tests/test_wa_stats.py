"""/admin/api/whatsapp/stats — who we ask Meta about, and when we ask at all.

The endpoint runs on every dashboard load. It used to fire two Graph calls
unconditionally, on the .env token, no matter which shop was looking:

1. no WhatsApp connected -> two guaranteed 401s per load
2. token expired         -> two 401s, the second one pointless
3. shop B's dashboard    -> shop A's template counts and quality rating

Every test here asserts on the recorded Graph calls, so "did not call Meta"
is a real assertion rather than an absence nobody checks.
"""

import pytest
from sqlalchemy import text as sqltext

import app.routers.agent_admin as aa
from app.config import settings
from app.database import async_session_factory
from app.models import ROLE_OWNER, User
from app.models.tenant import Tenant
from app.services import auth, tenant_context

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
STATS = "/admin/api/whatsapp/stats"

C_SLUG = "test-wa-stats-c"
C_PNID = "555000999888"
C_WABA = "waba-c-0001"
C_TOKEN = "EAAtestTokenForTenantC0000000000"
C_EMAIL = "wa-stats-c@test.local"
D_SLUG = "test-wa-stats-d"
D_EMAIL = "wa-stats-d@test.local"


@pytest.fixture(autouse=True)
def _clear_stats_cache():
    """The 60s cache is per-WABA and would otherwise leak between tests."""
    aa._wa_stats_cache.clear()
    yield
    aa._wa_stats_cache.clear()


@pytest.fixture(autouse=True)
def _home_env_creds(monkeypatch):
    """Pin the home shop's .env credentials for the duration of each test.

    Without this the file only passed on machines whose .env happened to
    carry a WABA id. .env.example does not ship one, so a fresh Codespace
    resolved no creds, the endpoint correctly called nobody, and four tests
    that expect Graph calls failed for a reason that had nothing to do with
    the code under test. A test that reads ambient config is a test that
    fails on someone else's machine.
    """
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN", "EAAhomeTokenForTests")
    monkeypatch.setattr(settings, "WHATSAPP_WABA_ID", "waba-home-9999")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "111000111000")


@pytest.fixture
def graph_calls(monkeypatch):
    """Record every Graph call. Default: both endpoints answer happily."""
    class Recorder(list):
        """The call log, with the canned replies hanging off it so a test can
        say graph_calls.replies['templates'] = (401, ...)."""

        replies = {
            "templates": (200, {"data": [
                {"name": "kk_ready", "status": "APPROVED"},
                {"name": "kk_offer", "status": "PENDING"},
            ]}),
            "phone": (200, {"quality_rating": "GREEN"}),
        }

    calls = Recorder()
    calls.replies = dict(Recorder.replies)

    async def fake_graph(method, path, token=None, **kw):
        calls.append({"method": method, "path": path, "token": token, **kw})
        if "message_templates" in path:
            return calls.replies["templates"]
        return calls.replies["phone"]

    monkeypatch.setattr(aa, "_graph", fake_graph)
    return calls


@pytest.fixture
async def tenant_c(client):
    """A second shop with its OWN WhatsApp credentials, logged in.

    The session cookie is the point: tenant_scope middleware resolves the
    tenant per request, so setting the ContextVar from the test does not
    reach the handler — only a real kk_session does.
    """
    async with async_session_factory() as db:
        t = Tenant(
            slug=C_SLUG, shop_name="Stats Test C", owner_name="C", plan="growth",
            owner_phone="+919999900077", status="active",
            wa_phone_number_id=C_PNID, wa_waba_id=C_WABA, wa_token=C_TOKEN,
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        u = User(
            tenant_id=t.id, email=C_EMAIL, name="C Owner",
            password_hash=auth.hash_password("cpass12345"), role=ROLE_OWNER,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        token = await auth.start_session(db, u)
    client.cookies.set(auth.SESSION_COOKIE, token)
    try:
        yield t
    finally:
        client.cookies.delete(auth.SESSION_COOKIE)
        tenant_context.current_tenant_id.set(None)
        async with async_session_factory() as db:
            for tbl in ("audit_log", "llm_usage", "conversations", "customers"):
                await db.execute(
                    sqltext(
                        f"DELETE FROM {tbl} WHERE tenant_id IN "
                        "(SELECT id FROM tenants WHERE slug = :s)"
                    ), {"s": C_SLUG},
                )
            await db.execute(
                sqltext(
                    "DELETE FROM login_sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE email = :e)"
                ), {"e": C_EMAIL},
            )
            await db.execute(sqltext("DELETE FROM users WHERE email = :e"), {"e": C_EMAIL})
            await db.execute(sqltext("DELETE FROM tenants WHERE slug = :s"), {"s": C_SLUG})
            await db.commit()


async def test_no_credentials_means_no_call_to_meta(client, graph_calls, monkeypatch) -> None:
    """The 401-per-load bug: an unconfigured shop must not ask Meta anything."""
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN", "")
    monkeypatch.setattr(settings, "WHATSAPP_WABA_ID", "")

    r = await client.get(STATS, headers=AUTH)

    assert r.status_code == 200
    assert graph_calls == [], "asked Meta despite having no credentials"
    body = r.json()
    assert body["meta_ok"] is False
    assert body["meta_state"] == "not_connected"
    # The DB half of the payload still has to work.
    assert set(body["today"]) == {"sent", "received", "customers_talked"}


async def test_auth_failure_skips_the_second_call(client, graph_calls) -> None:
    """A 401 on templates means the token is bad — asking for quality too
    was the second wasted call on every single load."""
    graph_calls.replies["templates"] = (401, {"error": {"code": 190}})

    r = await client.get(STATS, headers=AUTH)

    assert r.status_code == 200
    assert len(graph_calls) == 1, "second Graph call fired after a 401"
    body = r.json()
    assert body["meta_ok"] is False
    assert body["meta_state"] == "auth_failed"
    assert body["quality"] is None


async def test_healthy_token_gets_templates_and_quality(client, graph_calls) -> None:
    r = await client.get(STATS, headers=AUTH)

    assert r.status_code == 200
    assert len(graph_calls) == 2
    body = r.json()
    assert body["meta_ok"] is True and body["meta_state"] == "ok"
    assert body["templates"] == {"approved": 1, "pending": 1, "rejected": 0}
    assert body["quality"] == "GREEN"


async def test_second_load_inside_the_window_is_served_from_cache(client, graph_calls) -> None:
    """'Every dashboard load' was the actual complaint — two managers with
    the page open should not be two Graph calls each, every refresh."""
    await client.get(STATS, headers=AUTH)
    assert len(graph_calls) == 2

    r = await client.get(STATS, headers=AUTH)

    assert len(graph_calls) == 2, "cache did not hold the second load"
    assert r.json()["templates"] == {"approved": 1, "pending": 1, "rejected": 0}


async def test_a_shop_sees_its_own_waba_not_the_home_shops(
    client, graph_calls, tenant_c
) -> None:
    """The leak: stats always ran on the .env WABA, so every other shop was
    shown the home shop's templates and quality rating."""
    r = await client.get(STATS, headers=AUTH)

    assert r.status_code == 200
    templates_call = next(c for c in graph_calls if "message_templates" in c["path"])
    assert templates_call["path"].startswith(C_WABA), (
        f"queried {templates_call['path']} — expected shop C's own WABA"
    )
    assert templates_call["token"] == C_TOKEN, "used the home shop's token"
    assert settings.WHATSAPP_WABA_ID not in templates_call["path"]
    # The quality call must follow the same shop's number.
    phone_call = next(c for c in graph_calls if "message_templates" not in c["path"])
    assert phone_call["path"] == C_PNID and phone_call["token"] == C_TOKEN


async def test_unconnected_shop_does_not_fall_back_to_env_creds(
    client, graph_calls
) -> None:
    """.env creds belong to the HOME shop alone. A shop that never connected
    WhatsApp gets an honest empty state, not someone else's numbers."""
    async with async_session_factory() as db:
        t = Tenant(
            slug=D_SLUG, shop_name="Stats Test D", owner_name="D",
            plan="growth", owner_phone="+919999900078", status="active",
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        u = User(
            tenant_id=t.id, email=D_EMAIL, name="D Owner",
            password_hash=auth.hash_password("dpass12345"), role=ROLE_OWNER,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        token = await auth.start_session(db, u)

    client.cookies.set(auth.SESSION_COOKIE, token)
    try:
        r = await client.get(STATS, headers=AUTH)
    finally:
        client.cookies.delete(auth.SESSION_COOKIE)
        async with async_session_factory() as db:
            await db.execute(
                sqltext(
                    "DELETE FROM login_sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE email = :e)"
                ), {"e": D_EMAIL},
            )
            await db.execute(sqltext("DELETE FROM users WHERE email = :e"), {"e": D_EMAIL})
            await db.execute(sqltext("DELETE FROM tenants WHERE slug = :s"), {"s": D_SLUG})
            await db.commit()

    assert r.status_code == 200
    assert graph_calls == [], "fell back to the home shop's .env credentials"
    assert r.json()["meta_state"] == "not_connected"
