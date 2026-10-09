"""Google Business Profile reviews — parsing, website rendering, isolation.

Google ko kabhi call nahi hota: fetch/token functions monkeypatch hote hain.
"""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.services import app_settings, google_business as gbp, tenant_context
from tests.test_data_isolation import _tenant

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


def test_parse_review_maps_stars_anonymous_translation_and_reply() -> None:
    r = gbp.parse_review({
        "reviewId": "abc",
        "reviewer": {"displayName": "Riya", "isAnonymous": True, "profilePhotoUrl": "https://x/p.png"},
        "starRating": "FOUR",
        "comment": "(Translated by Google) Very good service\n\n(Original)\nBahut badhiya",
        "createTime": "2026-08-01T10:00:00Z",
        "reviewReply": {"comment": "Thank you!"},
    })
    assert r["rating"] == 4
    assert r["name"] == "A Google user"
    assert r["text"] == "Very good service"
    assert r["reply"] == "Thank you!"


def test_gbp_oauth_does_not_inherit_other_project_scopes() -> None:
    from urllib.parse import parse_qs, urlparse

    url = gbp.start_url("state-123", "https://kwikklin.online")
    params = parse_qs(urlparse(url).query)
    assert params["scope"] == [gbp.SCOPE]
    assert params["include_granted_scopes"] == ["false"]



def test_render_block_escapes_review_text_and_counts_stars() -> None:
    data = {
        "rating": 4.5, "count": 2,
        "reviews": [
            {"id": "1", "name": "<b>Evil</b>", "photo": "", "rating": 5,
             "text": "<script>alert(1)</script>", "time": "2026-09-01T00:00:00Z", "reply": ""},
            {"id": "2", "name": "Amit", "photo": "", "rating": 4, "text": "Good", "time": "2026-08-01T00:00:00Z", "reply": ""},
        ],
    }
    summary, cards = gbp.render_block(data)
    assert "4.5" in summary and "Based on 2 Google reviews" in summary
    assert "<script>" not in cards and "&lt;script&gt;" in cards
    assert "<b>Evil</b>" not in cards
    assert cards.index("alert") < cards.index("Good")  # newest first


def test_render_block_is_empty_without_reviews() -> None:
    assert gbp.render_block({}) == ("", "")


async def test_website_never_shows_another_shops_google_reviews(client) -> None:
    other = await _tenant("gbp-other-shop")
    async with tenant_context.as_tenant(other):
        async with async_session_factory() as db:
            await app_settings.set_value(db, "gbp_reviews", {
                "rating": 1.0, "count": 1,
                "reviews": [{"id": "x", "name": "Zzgbp Other", "photo": "", "rating": 1,
                             "text": "Zzgbp other shop review", "time": "2026-09-01T00:00:00Z", "reply": ""}],
            })
    try:
        gbp._home_cache.update(at=0.0, data=None)
        tok = tenant_context.current_tenant_id.set(other)  # jaise us dukaan ka owner page khole
        try:
            data = await gbp.home_reviews()
        finally:
            tenant_context.current_tenant_id.reset(tok)
        assert "Zzgbp" not in str(data)
        r = await client.get("/")
        assert r.status_code == 200 and "Zzgbp" not in r.text
        assert "{{GBP_" not in r.text
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM settings_kv WHERE tenant_id = :t"), {"t": other})
            await db.commit()
        gbp._home_cache.update(at=0.0, data=None)


async def test_connection_token_is_never_sent_to_browser_or_writable(client) -> None:
    r = await client.get("/admin/api/settings", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["gbp_connection"] in ({}, "••••••••")
    r = await client.put("/admin/api/settings", headers=AUTH,
                         json={"key": "gbp_connection", "value": {"refresh_token": "stolen"}})
    assert r.status_code == 400


async def test_failed_sync_keeps_old_reviews(monkeypatch) -> None:
    other = await _tenant("gbp-sync-shop")
    old = {"rating": 5.0, "count": 1, "reviews": [{"id": "1", "name": "A", "photo": "", "rating": 5,
                                                   "text": "old", "time": "", "reply": ""}]}

    async def boom(*a, **k):
        raise gbp.GBPError("quota is 0")

    monkeypatch.setattr(gbp, "fetch_reviews", boom)
    try:
        async with tenant_context.as_tenant(other):
            async with async_session_factory() as db:
                await gbp.save_connection(db, {"refresh_token": "rt", "account": "accounts/1", "location": "locations/2"})
                await app_settings.set_value(db, "gbp_reviews", old)
                with pytest.raises(gbp.GBPError):
                    await gbp.sync(db)
                stored = await app_settings.get(db, "gbp_reviews")
        assert stored["reviews"] == old["reviews"]
        assert stored["error"] == "quota is 0"
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM settings_kv WHERE tenant_id = :t"), {"t": other})
            await db.commit()


async def test_oauth_refresh_invalid_grant_is_actionable(monkeypatch) -> None:
    class FakeResponse:
        status_code = 400
        text = '{"error":"invalid_grant","error_description":"Token has been expired or revoked."}'
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "error": "invalid_grant",
                "error_description": "Token has been expired or revoked.",
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(gbp.httpx, "AsyncClient", FakeClient)

    with pytest.raises(gbp.GBPError, match="Reconnect Google"):
        await gbp._access_token("expired-refresh-token")


async def test_oauth_refresh_missing_access_token_is_rejected(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        text = "{}"
        headers = {"content-type": "application/json"}

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(gbp.httpx, "AsyncClient", FakeClient)

    with pytest.raises(gbp.GBPError, match="no access token"):
        await gbp._access_token("refresh-token")


async def test_status_marks_reauth_required_for_expired_refresh_token() -> None:
    from app.routers.google_business import status_for

    other = await _tenant("gbp-reauth-shop")
    try:
        async with tenant_context.as_tenant(other):
            async with async_session_factory() as db:
                await gbp.save_connection(db, {
                    "refresh_token": "rt",
                    "account": "accounts/1",
                    "location": "locations/2",
                    "title": "Reauth Shop",
                })
                await app_settings.set_value(db, "gbp_reviews", {
                    "error": "Google OAuth refresh token expired or was revoked. Reconnect Google.",
                })
                status = await status_for(db)

        assert status["connected"] is True
        assert status["reauth_required"] is True
        assert "Reconnect Google" in status["last_error"]
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM settings_kv WHERE tenant_id = :t"), {"t": other})
            await db.commit()


async def test_gbp_auto_post_records_failure_retries_and_deduplicates(monkeypatch) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services import gbp_auto_post

    other = await _tenant("gbp-post-retry-shop")
    calls = {"publish": 0, "generate": 0}

    async def fake_generate(db, now_ist):
        calls["generate"] += 1
        return {"summary": "A useful laundry care tip", "topic_type": "STANDARD"}

    async def fake_publish(*args, **kwargs):
        calls["publish"] += 1
        if calls["publish"] == 1:
            raise gbp.GBPError("temporary Google error")
        return {"name": "accounts/1/locations/2/localPosts/3"}

    async def fake_connection(db):
        return {"refresh_token": "dummy", "account": "accounts/1", "location": "locations/2"}

    monkeypatch.setattr(gbp, "enabled", lambda: True)
    monkeypatch.setattr(gbp, "get_connection", fake_connection)
    monkeypatch.setattr(gbp_auto_post, "_generate_post", fake_generate)
    monkeypatch.setattr(gbp_auto_post, "_api_post", fake_publish)
    now = datetime(2026, 10, 10, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    try:
        async with tenant_context.as_tenant(other):
            async with async_session_factory() as db:
                await app_settings.set_value(db, "gbp_auto_post_enabled", True)
                await app_settings.set_value(db, "gbp_auto_posts", [])
                with pytest.raises(gbp.GBPError, match="temporary Google error"):
                    await gbp_auto_post.auto_post_daily(db, now)
                history = await app_settings.get(db, "gbp_auto_posts")
                assert history[-1]["status"] == "failed"
                result = await gbp_auto_post.auto_post_daily(db, now)
                assert result["published"] is True
                again = await gbp_auto_post.auto_post_daily(db, now)
                assert again["reason"] == "already_published"
                history = await app_settings.get(db, "gbp_auto_posts")
                assert [item["status"] for item in history] == ["failed", "published"]
        assert calls == {"publish": 2, "generate": 2}
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM settings_kv WHERE tenant_id = :t"), {"t": other})
            await db.execute(sqltext("DELETE FROM tenants WHERE id = :t"), {"t": other})
            await db.commit()


async def test_gbp_daily_post_uses_tenant_shop_name(monkeypatch) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.models.tenant import Tenant
    from app.services import gbp_auto_post

    other = await _tenant("gbp-post-shop")
    captured = {}

    async def fake_ask_json(*, system, user_text, schema, model, max_tokens):
        captured["prompt"] = user_text
        return {"summary": "Daily laundry care tip", "topic_type": "STANDARD"}

    monkeypatch.setattr(gbp_auto_post.llm_client, "ask_json", fake_ask_json)
    try:
        async with tenant_context.as_tenant(other):
            async with async_session_factory() as db:
                tenant = await db.get(Tenant, other)
                tenant.shop_name = "Test Laundry Varanasi"
                await db.commit()
                await gbp_auto_post._generate_post(
                    db, datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
                )

        assert "- shop_name: Test Laundry Varanasi" in captured["prompt"]
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM settings_kv WHERE tenant_id = :t"), {"t": other})
            await db.execute(sqltext("DELETE FROM tenants WHERE id = :t"), {"t": other})
            await db.commit()
