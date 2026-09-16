"""Website (/laundry) ke liye live Google reviews — Places API (New).

Har visitor par Google ko call nahi: 6 ghante ka cache, kyunki Places API
har request ka paisa leta hai aur reviews ghante mein nahi badalte. Fail
hone par 10 minute ka chhota cache, taaki Google down ho to har page load
us par atke nahi.

Google ki shart: review ke saath author ka naam/link aur "Google" attribution
dikhana zaroori hai — isliye wo fields bhi bahar jaati hain.
"""

import time

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

_API = "https://places.googleapis.com/v1"
_FIELDS = "id,displayName,rating,userRatingCount,reviews,googleMapsUri"
_SEARCH_QUERY = "Kwik Klin Smart Online Laundry Services, Sundarpur Chauraha, Varanasi"
_TTL_OK = 6 * 3600
_TTL_FAIL = 600

_cache: dict = {"at": 0.0, "ttl": 0, "data": None}


def _clean(place: dict) -> dict:
    reviews = []
    for r in place.get("reviews") or []:
        text = (r.get("text") or r.get("originalText") or {}).get("text", "").strip()
        if not text:
            continue
        author = r.get("authorAttribution") or {}
        reviews.append({
            "rating": r.get("rating"),
            "text": text,
            "when": r.get("relativePublishTimeDescription", ""),
            "author": author.get("displayName", "Google user"),
            "author_url": author.get("uri", ""),
            "author_photo": author.get("photoUri", ""),
        })
    return {
        "configured": True,
        "rating": place.get("rating"),
        "count": place.get("userRatingCount"),
        "maps_url": place.get("googleMapsUri", ""),
        "reviews": reviews,
    }


async def _fetch() -> dict | None:
    headers = {"X-Goog-Api-Key": settings.GOOGLE_PLACES_API_KEY}
    async with httpx.AsyncClient(timeout=10) as client:
        if settings.GOOGLE_PLACE_ID:
            r = await client.get(
                f"{_API}/places/{settings.GOOGLE_PLACE_ID}",
                params={"languageCode": "en"},
                headers={**headers, "X-Goog-FieldMask": _FIELDS},
            )
            r.raise_for_status()
            return r.json()
        r = await client.post(
            f"{_API}/places:searchText",
            json={"textQuery": _SEARCH_QUERY, "languageCode": "en"},
            headers={**headers, "X-Goog-FieldMask": ",".join(f"places.{f}" for f in _FIELDS.split(","))},
        )
        r.raise_for_status()
        places = r.json().get("places") or []
        return places[0] if places else None


async def get_reviews() -> dict:
    if not settings.GOOGLE_PLACES_API_KEY:
        return {"configured": False}
    now = time.monotonic()
    if _cache["data"] is not None and now - _cache["at"] < _cache["ttl"]:
        return _cache["data"]
    try:
        place = await _fetch()
        data = _clean(place) if place else {"configured": True, "reviews": []}
        ttl = _TTL_OK
    except Exception:
        log.exception("google_reviews_fetch_failed")
        # Purana achha data ho to wahi dikhate raho
        data = _cache["data"] or {"configured": True, "reviews": []}
        ttl = _TTL_FAIL
    _cache.update(at=now, ttl=ttl, data=data)
    return data
