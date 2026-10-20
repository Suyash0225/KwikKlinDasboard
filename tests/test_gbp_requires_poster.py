from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock

from sqlalchemy import text as sqltext

from app.database import async_session_factory
from app.services import app_settings, google_business as gbp, tenant_context
from app.services import gbp_auto_post
from tests.test_data_isolation import _tenant


async def test_gbp_never_generates_poster_when_owner_poster_missing(monkeypatch):
    tenant_id = await _tenant("gbp-owner-poster-only")
    publish_calls = []
    poster_generation_calls = []

    async def fake_connection(db):
        return {
            "refresh_token": "dummy",
            "account": "accounts/1",
            "location": "locations/101",
            "post_locations": [
                {"account": "accounts/1", "location": "locations/101", "title": "Lanka"},
                {"account": "accounts/1", "location": "locations/102", "title": "Sigra"},
                {"account": "accounts/1", "location": "locations/103", "title": "Bhelupur"},
            ],
        }

    async def fake_generate(db, now_ist):
        return {"summary": "Useful laundry care tip", "topic_key": "stain-care"}

    async def forbidden_poster_generation(*args, **kwargs):
        poster_generation_calls.append((args, kwargs))
        raise AssertionError("Automatic poster generation must remain disabled")

    async def fake_publish(*args, **kwargs):
        publish_calls.append((args, kwargs))
        return {"name": "unexpected/post"}

    monkeypatch.setattr(gbp, "enabled", lambda: True)
    monkeypatch.setattr(gbp, "get_connection", fake_connection)
    monkeypatch.setattr(gbp_auto_post, "_generate_post", fake_generate)
    monkeypatch.setattr(gbp_auto_post, "_build_poster_url", forbidden_poster_generation)
    monkeypatch.setattr(gbp_auto_post, "_api_post", fake_publish)
    monkeypatch.setattr(gbp_auto_post.audit, "record", AsyncMock())

    now = datetime(2026, 10, 20, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
    try:
        async with tenant_context.as_tenant(tenant_id):
            async with async_session_factory() as db:
                await app_settings.set_value(db, "gbp_auto_post_enabled", True)
                await app_settings.set_value(db, "gbp_owner_poster_url", "")
                await app_settings.set_value(db, "gbp_auto_posts", [])
                result = await gbp_auto_post.auto_post_daily(db, now)
                history = await app_settings.get(db, "gbp_auto_posts")

        assert result["published"] is False
        assert result["published_count"] == 0
        assert result["poster_attached"] is False
        assert len(result["failed_locations"]) == 3
        assert publish_calls == []
        assert poster_generation_calls == []
        assert [item["status"] for item in history] == ["failed", "failed", "failed"]
        assert all(item["poster_attached"] is False for item in history)
        assert all("owner_poster_required" in item["error"] for item in history)
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM settings_kv WHERE tenant_id = :t"), {"t": tenant_id})
            await db.execute(sqltext("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
            await db.commit()
