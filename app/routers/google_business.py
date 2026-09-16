"""Dashboard -> Settings -> "Google reviews on your website" — SIRF haal.

    GET  /admin/api/google-business/status   kya juda hai, aakhri sync, error

Jodna, listing chunna, sync aur hatana vendor Control panel se hota hai
(routers/control.py: /control/api/tenants/{slug}/google-business/...), taaki
token ka koi bhi kaam dukaan ke browser se na ho. Status owner ke APNE
tenant ka hi hota hai (middleware session se context set karta hai).
"""

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


async def status_for(db: AsyncSession) -> dict:
    """Current tenant ka Google haal — token kabhi nahi."""
    conn = await gbp.get_connection(db)
    data = dict(await app_settings.get(db, "gbp_reviews") or {})
    return {
        "configured": gbp.enabled(),
        "connected": bool(conn.get("refresh_token")),
        "location": conn.get("location", ""),
        "title": conn.get("title", ""),
        "choices": [] if conn.get("location") else conn.get("choices", []),
        "last_error": conn.get("last_error", "") or data.get("error", ""),
        "rating": data.get("rating"),
        "count": data.get("count"),
        "stored": len(data.get("reviews") or []),
        "synced_at": data.get("synced_at", ""),
    }


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db)) -> dict:
    return await status_for(db)
