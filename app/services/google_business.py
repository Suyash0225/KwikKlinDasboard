"""Google Business Profile — dukaan ke SAARE Google reviews, uski apni listing se.

Places API sirf 5 reviews deta hai (Google ki marzi ke). Business Profile API
listing ke MALIK ko har review deta hai — naam, stars, text, date, aur malik
ka jawab. Isliye ye rasta: Control panel se (dukaan ke Google account se)
ek baar "Connect Google" hota hai, hum refresh token rakhte hain, aur har 6 ghante reviews kheench kar
settings_kv (`gbp_reviews`) mein rakh dete hain. Website DB se padhti hai —
har visitor par Google ko call nahi.

Per-tenant hai: har dukaan apni listing jod sakti hai. /laundry sirf HOME
dukaan ka data dikhata hai (dekhein home_reviews()).

Google ki taraf se do shartein (code se bahar, ek baar):
1. Cloud project ko "Business Profile API" access approve hona chahiye
   (form bharna padta hai). Tab tak har call 403/429 — status mein saaf
   message dikhta hai, kuch tootta nahi.
2. OAuth client mein redirect URI: <public URL>/control/api/google-business/callback
"""

import html as _html
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import structlog

from app.config import settings
from app.services import google_auth, secrets

log = structlog.get_logger()

SCOPE = "https://www.googleapis.com/auth/business.manage"
STATE_COOKIE = "kk_gbp_state"
_ACCOUNTS = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
_INFO = "https://mybusinessbusinessinformation.googleapis.com/v1"
_REVIEWS = "https://mybusiness.googleapis.com/v4"
_STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
MAX_STORED = 300


class GBPError(Exception):
    """Owner ko dikhane layak wajah."""


def enabled() -> bool:
    return google_auth.enabled()


def redirect_uri(base: str) -> str:
    # Vendor Control panel ka callback — dukaan ka owner dashboard Google
    # nahi jodta (routers/control.py)
    return base.rstrip("/") + "/control/api/google-business/callback"


def start_url(state: str, base: str) -> str:
    return google_auth.AUTH_URL + "?" + urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(base),
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        # offline + consent: refresh token har baar milta hai, warna doosri
        # baar connect karne par Google use dobara nahi deta
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
    })


def _explain(r: httpx.Response) -> str:
    try:
        err = (r.json() or {}).get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        msg = r.text[:200]
    if r.status_code == 429 or "quota" in (msg or "").lower():
        return ("Google has not enabled Business Profile API access for this project yet "
                "(quota is 0). Request access from Google, then try again.")
    if r.status_code == 403:
        return ("Google refused access. Make sure the Business Profile APIs are enabled and "
                "approved for this project, and that this Google account manages the listing.")
    if r.status_code == 401:
        return "Google connection expired. Please connect Google again."
    return f"Google error {r.status_code}: {msg or 'unknown'}"


async def exchange_code(code: str, base: str) -> str:
    """code -> refresh token."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(google_auth.TOKEN_URL, data={
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri(base),
            "grant_type": "authorization_code",
        })
    if r.status_code != 200:
        log.warning("gbp_token_exchange_failed", status=r.status_code, body=r.text[:200])
        raise GBPError("Google did not confirm the connection. Please try again.")
    data = r.json() or {}
    if SCOPE not in (data.get("scope") or ""):
        raise GBPError("Permission to manage your Business Profile was not granted.")
    if not data.get("refresh_token"):
        raise GBPError("Google did not return long-term access. Please try connecting again.")
    return data["refresh_token"]


async def _access_token(refresh_token: str) -> str:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(google_auth.TOKEN_URL, data={
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
    if r.status_code != 200:
        log.warning("gbp_refresh_failed", status=r.status_code, body=r.text[:200])
        raise GBPError("Google connection expired or was removed. Please connect Google again.")
    return r.json()["access_token"]


async def _paged(c: httpx.AsyncClient, url: str, params: dict, key: str) -> list[dict]:
    out, token = [], None
    for _ in range(50):  # hard stop — kabhi infinite loop nahi
        r = await c.get(url, params={**params, **({"pageToken": token} if token else {})})
        if r.status_code != 200:
            raise GBPError(_explain(r))
        body = r.json() or {}
        out.extend(body.get(key) or [])
        token = body.get("nextPageToken")
        if not token:
            return out
    return out


async def list_locations(refresh_token: str) -> list[dict]:
    """Is Google account ki saari listings: [{account, location, title, address}]."""
    tok = await _access_token(refresh_token)
    async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {tok}"}) as c:
        accounts = await _paged(c, _ACCOUNTS, {"pageSize": 20}, "accounts")
        out = []
        for acc in accounts:
            locs = await _paged(
                c, f"{_INFO}/{acc['name']}/locations",
                {"readMask": "name,title,storefrontAddress", "pageSize": 100}, "locations",
            )
            for loc in locs:
                addr = loc.get("storefrontAddress") or {}
                out.append({
                    "account": acc["name"],          # "accounts/123"
                    "location": loc["name"],         # "locations/456"
                    "title": loc.get("title") or "",
                    "address": ", ".join(filter(None, [
                        *(addr.get("addressLines") or []), addr.get("locality"), addr.get("postalCode"),
                    ])),
                })
        return out


def parse_review(r: dict) -> dict:
    who = r.get("reviewer") or {}
    reply = r.get("reviewReply") or {}
    return {
        "id": r.get("reviewId") or r.get("name", ""),
        "name": "A Google user" if who.get("isAnonymous") else (who.get("displayName") or "A Google user"),
        "photo": who.get("profilePhotoUrl") or "",
        "rating": _STARS.get(r.get("starRating") or "", 0),
        "text": (r.get("comment") or "").split("\n\n(Original)")[0].replace("(Translated by Google) ", "").strip(),
        "time": r.get("createTime") or r.get("updateTime") or "",
        "reply": (reply.get("comment") or "").strip(),
    }


async def fetch_reviews(refresh_token: str, account: str, location: str) -> dict:
    tok = await _access_token(refresh_token)
    loc_id = location.split("/")[-1]
    url = f"{_REVIEWS}/{account}/locations/{loc_id}/reviews"
    async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {tok}"}) as c:
        reviews, token, head = [], None, {}
        for _ in range(20):
            r = await c.get(url, params={"pageSize": 50, "orderBy": "updateTime desc",
                                          **({"pageToken": token} if token else {})})
            if r.status_code != 200:
                raise GBPError(_explain(r))
            body = r.json() or {}
            head = head or body
            reviews.extend(body.get("reviews") or [])
            token = body.get("nextPageToken")
            if not token or len(reviews) >= MAX_STORED:
                break
    parsed = [parse_review(x) for x in reviews[:MAX_STORED]]
    return {
        "rating": head.get("averageRating"),
        "count": head.get("totalReviewCount", len(parsed)),
        "reviews": parsed,
    }


# ---------------------------------------------------------------------------
# Tenant state (settings_kv) — current tenant context mein
# ---------------------------------------------------------------------------

async def get_connection(db) -> dict:
    from app.services import app_settings

    conn = dict(await app_settings.get(db, "gbp_connection") or {})
    if conn.get("refresh_token"):
        conn["refresh_token"] = secrets.decrypt(conn["refresh_token"])
    return conn


async def save_connection(db, conn: dict) -> None:
    from app.services import app_settings

    stored = dict(conn)
    if stored.get("refresh_token"):
        stored["refresh_token"] = secrets.encrypt(stored["refresh_token"])
    await app_settings.set_value(db, "gbp_connection", stored)


async def sync(db) -> dict:
    """Current tenant ke reviews Google se laao aur rakho. Fail ho to purane
    reviews waise hi rehte hain, sirf error likha jaata hai."""
    from app.services import app_settings

    conn = await get_connection(db)
    if not (conn.get("refresh_token") and conn.get("location")):
        raise GBPError("Google is not connected yet.")
    old = dict(await app_settings.get(db, "gbp_reviews") or {})
    now = datetime.now(timezone.utc).isoformat()
    try:
        data = await fetch_reviews(conn["refresh_token"], conn["account"], conn["location"])
    except GBPError as exc:
        old.update(error=str(exc), attempted_at=now)
        await app_settings.set_value(db, "gbp_reviews", old)
        raise
    data.update(synced_at=now, error="", title=conn.get("title", ""))
    await app_settings.set_value(db, "gbp_reviews", data)
    log.info("gbp_reviews_synced", count=len(data["reviews"]), rating=data.get("rating"))
    return data


async def revoke(refresh_token: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post("https://oauth2.googleapis.com/revoke", data={"token": refresh_token})
    except Exception:
        log.warning("gbp_revoke_failed")


# ---------------------------------------------------------------------------
# Website
# ---------------------------------------------------------------------------

_home_cache: dict = {"at": 0.0, "data": None}


async def home_reviews() -> dict:
    """HOME dukaan ke stored reviews — dekhne wala kisi bhi dukaan ka ho."""
    from app.database import async_session_factory
    from app.services import app_settings, tenant_context

    now = time.monotonic()
    if _home_cache["data"] is not None and now - _home_cache["at"] < 60:
        return _home_cache["data"]
    data: dict = {}
    try:
        home = await tenant_context.get_home_tenant_id()
        if home is not None:
            async with tenant_context.as_tenant(home):
                async with async_session_factory() as db:
                    data = dict(await app_settings.get(db, "gbp_reviews") or {})
    except Exception:
        log.exception("gbp_home_reviews_failed")
        data = _home_cache["data"] or {}
    _home_cache.update(at=now, data=data)
    return data


def _stars(n: float) -> str:
    k = int(round(n or 0))
    return "★" * k + "☆" * (5 - k)


def _ago(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    days = (datetime.now(timezone.utc) - t).days
    if days < 1:
        return "today"
    for size, unit in ((365, "year"), (30, "month"), (7, "week"), (1, "day")):
        if days >= size:
            n = days // size
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return ""


GOOGLE_G = ('<svg class="glogo" viewBox="0 0 48 48" aria-hidden="true"><path fill="#FFC107" d="M43.6 20.1H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.6-.4-3.9z"/>'
            '<path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/>'
            '<path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z"/>'
            '<path fill="#1976D2" d="M43.6 20.1H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.6-.4-3.9z"/></svg>')

VISIBLE = 6  # itne card seedhe dikhte hain, baaki "Show more" ke peeche (HTML mein phir bhi)


def render_block(data: dict) -> tuple[str, str]:
    """(summary_html, cards_html). Data na ho to ('', '') — page purana
    map-embed wala rasta hi dikhata hai."""
    reviews = [r for r in (data or {}).get("reviews") or [] if r.get("rating")]
    if not reviews:
        return "", ""
    esc = _html.escape
    rating = float(data.get("rating") or sum(r["rating"] for r in reviews) / len(reviews))
    count = int(data.get("count") or len(reviews))
    dist = {s: sum(1 for r in reviews if r["rating"] == s) for s in range(5, 0, -1)}
    bars = "".join(
        f'<div class="bar"><span>{s}★</span><i><b style="width:{round(100 * n / len(reviews))}%"></b></i><span>{n}</span></div>'
        for s, n in dist.items()
    )
    summary = (
        f'<div class="rsum">{GOOGLE_G}'
        f'<div class="rbig"><b>{rating:.1f}</b><span class="stars" aria-label="{rating:.1f} out of 5">{_stars(rating)}</span>'
        f'<small>Based on {count:,} Google reviews</small></div>'
        f'<div class="bars">{bars}</div></div>'
    )

    shown = sorted(
        (r for r in reviews if r["text"]),
        key=lambda r: r.get("time") or "", reverse=True,
    )
    cards = []
    for i, r in enumerate(shown):
        av = (f'<img src="{esc(r["photo"])}" alt="" width="40" height="40" loading="lazy" referrerpolicy="no-referrer">'
              if r.get("photo") else f'<span class="av">{esc((r["name"] or "G")[0].upper())}</span>')
        reply = (f'<div class="reply"><b>Response from the owner</b>{esc(r["reply"])}</div>' if r.get("reply") else "")
        cards.append(
            f'<article class="review"{" hidden data-more" if i >= VISIBLE else ""}>'
            f'<div class="stars" aria-label="{r["rating"]} out of 5 stars">{_stars(r["rating"])}</div>'
            f'<p>{esc(r["text"])}</p>{reply}'
            f'<div class="who">{av}<div><b>{esc(r["name"])}</b><small>{esc(_ago(r["time"]))} · Google</small></div>{GOOGLE_G}</div>'
            f'</article>'
        )
    more = (f'<div class="more-wrap"><button class="btn btn-ghost" type="button" id="rev-more">'
            f'Show all {len(shown)} reviews</button></div>' if len(shown) > VISIBLE else "")
    return summary, f'<div class="reviews" data-gbp>{"".join(cards)}</div>{more}'
