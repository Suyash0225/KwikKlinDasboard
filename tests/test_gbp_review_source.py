"""The owner picks whose Google reviews the website shows."""

from app.config import settings
from app.database import async_session_factory
from app.services import google_business as gbp

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
CONN = {
    "refresh_token": "rt", "account": "accounts/1", "location": "locations/101", "title": "Sundarpur",
    "post_locations": [
        {"account": "accounts/1", "location": "locations/101", "title": "Sundarpur"},
        {"account": "accounts/1", "location": "locations/102", "title": "Lanka"},
        {"account": "accounts/1", "location": "locations/103", "title": "Sigra"},
    ],
}


def test_targets_default_one_and_all() -> None:
    assert [t["title"] for t in gbp.review_targets(CONN)[0]] == ["Sundarpur"]
    assert [t["title"] for t in gbp.review_targets({**CONN, "review_source": "locations/102"})[0]] == ["Lanka"]
    targets, title = gbp.review_targets({**CONN, "review_source": "all"})
    assert [t["title"] for t in targets] == ["Sundarpur", "Lanka", "Sigra"] and title == "All locations"
    # an unknown choice falls back to the main listing, never errors
    assert [t["title"] for t in gbp.review_targets({**CONN, "review_source": "locations/999"})[0]] == ["Sundarpur"]


def test_merge_weights_rating_by_review_count_and_sorts_newest_first() -> None:
    merged = gbp.merge_reviews([
        {"rating": 4.6, "count": 200, "reviews": [{"time": "2026-10-01", "rating": 5, "text": "a"}]},
        {"rating": 5.0, "count": 3, "reviews": [{"time": "2026-10-10", "rating": 5, "text": "b"}]},
    ])
    assert merged["count"] == 203
    assert 4.6 <= merged["rating"] < 4.62, "3 reviews at 5.0 must not drag 200 at 4.6 up"
    assert [r["text"] for r in merged["reviews"]] == ["b", "a"]


async def test_choosing_a_listing_saves_and_resyncs(client, monkeypatch) -> None:
    async with async_session_factory() as db:
        await gbp.save_connection(db, dict(CONN))
    fetched = []

    async def fake_fetch(rt, account, location):
        fetched.append(location)
        return {"rating": 4.8, "count": 10, "reviews": [{"time": "2026-10-1" + location[-1], "rating": 5, "text": location, "name": "x"}]}

    monkeypatch.setattr(gbp, "fetch_reviews", fake_fetch)
    r = await client.put("/admin/api/google-business/review-source", headers=AUTH, json={"source": "all"})
    assert r.status_code == 200 and r.json()["synced"] is True
    assert fetched == ["locations/101", "locations/102", "locations/103"]
    assert r.json()["review_source"] == "all" and len(r.json()["review_locations"]) == 3
    bad = await client.put("/admin/api/google-business/review-source", headers=AUTH, json={"source": "locations/999"})
    assert bad.status_code == 400
