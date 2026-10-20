"""The poster dialog shows what was published — every attempt is logged."""

from app.config import settings
from app.database import async_session_factory
from app.routers import gbp_poster_admin
from app.services import app_settings

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
POSTER = "https://example.test/poster.jpg"


async def _set(key, value):
    async with async_session_factory() as db:
        await app_settings.set_value(db, key, value)


async def test_publish_attempts_are_logged_newest_first(client, monkeypatch):
    await _set("gbp_owner_poster_url", POSTER)
    await _set("gbp_owner_post_log", [])
    monkeypatch.setattr(gbp_poster_admin.gbp, "enabled", lambda: True)

    async def same(caption):
        return caption

    results = iter([
        {"published": ["Lanka", "Sigra"], "failed": []},
        {"published": ["Lanka"], "failed": ["Sigra: quota"]},
    ])

    async def fake_publish(db, summary, url, action):
        return next(results)

    monkeypatch.setattr(gbp_poster_admin.gbp_owner_post, "_interpret_owner_caption", same)
    monkeypatch.setattr(gbp_poster_admin.gbp_owner_post, "publish_owner_post", fake_publish)

    for caption in ("WEBSITE: Diwali saree care", "CALL NOW: Blanket season"):
        r = await client.post("/admin/api/google-business/poster/publish", headers=AUTH, data={"caption": caption})
        assert r.status_code == 200

    h = (await client.get("/admin/api/google-business/poster/history", headers=AUTH)).json()["items"]
    assert [x["status"] for x in h[:2]] == ["partial", "published"]
    assert h[0]["failed"] == ["Sigra: quota"] and h[1]["published"] == ["Lanka", "Sigra"]
    assert h[0]["poster_url"] == POSTER


async def test_a_rejected_publish_is_logged_too(client, monkeypatch):
    await _set("gbp_owner_poster_url", POSTER)
    await _set("gbp_owner_post_log", [])
    monkeypatch.setattr(gbp_poster_admin.gbp, "enabled", lambda: False)
    r = await client.post("/admin/api/google-business/poster/publish", headers=AUTH, data={"caption": "WEBSITE: hello there"})
    assert r.status_code == 503
    h = (await client.get("/admin/api/google-business/poster/history", headers=AUTH)).json()["items"]
    assert h[0]["status"] == "failed" and "not configured" in h[0]["failed"][0]
