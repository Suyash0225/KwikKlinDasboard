"""Dashboard -> Settings -> "Google reviews on your website" — SIRF haal.

    GET  /admin/api/google-business/status   kya juda hai, aakhri sync, error

Jodna, listing chunna, sync aur hatana vendor Control panel se hota hai
(routers/control.py: /control/api/tenants/{slug}/google-business/...), taaki
token ka koi bhi kaam dukaan ke browser se na ho. Status owner ke APNE
tenant ka hi hota hai (middleware session se context set karta hai).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routers.orders import require_admin_owner
from app.services import app_settings
from app.services import google_business as gbp

router = APIRouter(
    prefix="/admin/api/google-business",
    tags=["google-business"],
    dependencies=[Depends(require_admin_owner)],
)


def _post_location_statuses(locations: list[dict], history: list[dict], primary_location: str = "") -> list[dict]:
    """Return the latest post result for each selected location, without credentials."""
    latest = {}
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        location = str(item.get("location") or primary_location)
        account = str(item.get("account") or "*")
        if location and (account, location) not in latest:
            latest[(account, location)] = item

    result = []
    seen = set()
    for target in locations:
        if not isinstance(target, dict) or not target.get("location"):
            continue
        location = str(target["location"])
        account = str(target.get("account") or "")
        key = (account, location)
        if key in seen:
            continue
        seen.add(key)
        item = latest.get(key) or latest.get(("*", location))
        result.append({
            "location": location,
            "title": str(target.get("title") or location),
            "address": str(target.get("address") or ""),
            "status": str(item.get("status") or "not_posted") if item else "not_posted",
            "date": str(item.get("date") or "") if item else "",
            "updated_at": str(item.get("published_at") or item.get("attempted_at") or "") if item else "",
            "post_name": str(item.get("post_name") or "") if item else "",
            "error": str(item.get("error") or "")[:240] if item else "",
            "poster_attached": bool(item.get("poster_attached")) if item else False,
            "poster_issue": str(item.get("poster_issue") or "")[:180] if item else "",
        })
    return result


async def status_for(db: AsyncSession) -> dict:
    """Current tenant ka Google haal — token kabhi nahi."""
    conn = await gbp.get_connection(db)
    data = dict(await app_settings.get(db, "gbp_reviews") or {})
    last_error = conn.get("last_error", "") or data.get("error", "")
    reauth_required = (
        "refresh token expired or was revoked" in str(last_error).lower()
        or "oauth refresh token" in str(last_error).lower()
    )
    auto_post = await __import__("app.services.gbp_auto_post", fromlist=["status"]).status(db)
    post_locations = conn.get("post_locations") or (
        [{"account": conn.get("account", ""), "location": conn.get("location", ""),
          "title": conn.get("title", ""), "address": conn.get("address", "")}]
        if conn.get("location") else []
    )
    return {
        "configured": gbp.enabled(),
        "connected": bool(conn.get("refresh_token")),
        "reauth_required": reauth_required,
        "location": conn.get("location", ""),
        "title": conn.get("title", ""),
        "choices": [] if conn.get("location") else conn.get("choices", []),
        "last_error": last_error,
        "rating": data.get("rating"),
        "count": data.get("count"),
        "stored": len(data.get("reviews") or []),
        "synced_at": data.get("synced_at", ""),
        "auto_post": auto_post,
        "auto_post_locations": _post_location_statuses(
            post_locations, auto_post.get("history") or [], str(conn.get("location") or "")
        ),
        "auto_post_location_count": len(post_locations),
    }


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db)) -> dict:
    return await status_for(db)



@router.put("/auto-post")
async def set_auto_post(enabled: bool, db: AsyncSession = Depends(get_db)) -> dict:
    """Turn daily AI Google Business Profile posting on/off."""
    await app_settings.set_value(db, "gbp_auto_post_enabled", bool(enabled))
    from app.services import gbp_auto_post
    return await gbp_auto_post.status(db)


@router.post("/auto-post/run")
async def run_auto_post(db: AsyncSession = Depends(get_db)) -> dict:
    """Run today's post immediately (useful for first-time verification)."""
    from app.services import gbp_auto_post
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    return await gbp_auto_post.auto_post_daily(db, now_ist)
