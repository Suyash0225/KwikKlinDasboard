"""Tenant-scoped GA4 credential access used by reporting adapters."""
import httpx
from app.config import settings
from app.services import app_settings, secrets
from app.services.analytics import AnalyticsError


async def connection(db) -> dict:
    conn = dict(await app_settings.get(db, "ga4_connection") or {})
    if conn.get("refresh_token"):
        conn["refresh_token"] = secrets.decrypt(conn["refresh_token"])
    return conn


async def access_token(db) -> str:
    conn = await connection(db)
    token = conn.get("refresh_token")
    if not (token and settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
        raise AnalyticsError("GA4 reporting is not connected")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "refresh_token": token,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code != 200:
        raise AnalyticsError("GA4 Google authorization expired or lacks analytics.readonly")
    return r.json()["access_token"]
