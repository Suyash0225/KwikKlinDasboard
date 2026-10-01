"""Google Analytics 4 OAuth connection for the manager dashboard."""
import secrets as _secrets
from urllib.parse import urlencode

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.routers.orders import require_admin_owner
from app.services import app_settings, google_auth, secrets

router = APIRouter(prefix="/admin/api/ga4", tags=["google-analytics"])
log = structlog.get_logger()

SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
ADMIN_API = "https://analyticsadmin.googleapis.com/v1beta/accountSummaries"


class PropertyPick(BaseModel):
    property_id: str


def _redirect_uri(base: str) -> str:
    return base.rstrip("/") + "/admin/api/ga4/callback"


def _safe(conn: dict) -> dict:
    return {
        "connected": bool(conn.get("refresh_token") and conn.get("property_id")),
        "property_id": conn.get("property_id") or "",
        "property_name": conn.get("property_name") or "",
        "choices": [
            {"property_id": str(x.get("property_id") or ""),
             "property_name": str(x.get("property_name") or "")}
            for x in (conn.get("choices") or []) if x.get("property_id")
        ],
        "last_error": conn.get("last_error") or "",
    }


async def _get(db) -> dict:
    conn = dict(await app_settings.get(db, "ga4_connection") or {})
    if conn.get("refresh_token"):
        conn["refresh_token"] = secrets.decrypt(conn["refresh_token"])
    return conn


async def _save(db, conn: dict) -> None:
    stored = dict(conn)
    if stored.get("refresh_token"):
        stored["refresh_token"] = secrets.encrypt(stored["refresh_token"])
    await app_settings.set_value(db, "ga4_connection", stored)


async def _access_token(refresh_token: str) -> str:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            google_auth.TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code != 200:
        raise HTTPException(status_code=400, detail="Google Analytics connection expired. Connect GA4 again.")
    return r.json()["access_token"]


async def _list_properties(token: str) -> list[dict]:
    out = []
    page_token = ""
    async with httpx.AsyncClient(timeout=20) as c:
        for _ in range(10):
            params = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            r = await c.get(
                ADMIN_API,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            if r.status_code != 200:
                log.warning("ga4_property_list_failed", status=r.status_code, body=r.text[:300])
                raise HTTPException(
                    status_code=400,
                    detail="Google Analytics access could not be verified. Enable Analytics Admin/Data APIs and try again.",
                )
            body = r.json() or {}
            for account in body.get("accountSummaries") or []:
                for prop in account.get("propertySummaries") or []:
                    raw = str(prop.get("property") or "")
                    pid = raw.removeprefix("properties/")
                    if pid:
                        out.append({"property_id": pid, "property_name": str(prop.get("displayName") or pid)})
            page_token = str(body.get("nextPageToken") or "")
            if not page_token:
                break
    seen = set()
    return [x for x in out if not (x["property_id"] in seen or seen.add(x["property_id"]))]


@router.get("/status", dependencies=[Depends(require_admin_owner)])
async def status(db: AsyncSession = Depends(get_db)) -> dict:
    return _safe(await _get(db))


@router.post("/connect-url", dependencies=[Depends(require_admin_owner)])
async def connect_url(db: AsyncSession = Depends(get_db)) -> dict:
    if not google_auth.enabled():
        raise HTTPException(status_code=503, detail="Google OAuth is not configured on the server")
    state = _secrets.token_urlsafe(32)
    conn = await _get(db)
    conn["oauth_state"] = state
    await _save(db, conn)
    base = await google_auth.public_base(db)
    url = google_auth.AUTH_URL + "?" + urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": _redirect_uri(base),
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
    })
    return {"url": url}


@router.get("/callback")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: AsyncSession = Depends(get_db),
):
    conn = await _get(db)
    expected = str(conn.get("oauth_state") or "")
    conn.pop("oauth_state", None)
    if not expected or not state or not _secrets.compare_digest(state, expected):
        raise HTTPException(status_code=400, detail="GA4 connection expired. Start Connect GA4 again.")
    if error:
        await _save(db, conn)
        return RedirectResponse("/admin?probe=1#dashboard", status_code=303)
    if not code:
        raise HTTPException(status_code=400, detail="Google did not return an authorization code.")

    base = await google_auth.public_base(db)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            google_auth.TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": _redirect_uri(base),
                "grant_type": "authorization_code",
            },
        )
    if r.status_code != 200:
        log.warning("ga4_token_exchange_failed", status=r.status_code, body=r.text[:300])
        raise HTTPException(status_code=400, detail="Google did not confirm the GA4 connection.")

    data = r.json() or {}
    if SCOPE not in set(str(data.get("scope") or "").split()):
        raise HTTPException(status_code=400, detail="Google Analytics read permission was not granted.")
    refresh = str(data.get("refresh_token") or "")
    token = str(data.get("access_token") or "")
    if not refresh or not token:
        raise HTTPException(status_code=400, detail="Google did not return a usable GA4 token. Connect again.")

    props = await _list_properties(token)
    if not props:
        raise HTTPException(status_code=400, detail="This Google account has no accessible GA4 properties.")

    old_property = conn.get("property_id")
    conn.update({
        "refresh_token": refresh,
        "property_id": "",
        "property_name": "",
        "choices": props,
        "last_error": "",
    })
    if len(props) == 1:
        conn.update(props[0])
        conn["choices"] = []
    elif old_property and any(x["property_id"] == old_property for x in props):
        conn.update(next(x for x in props if x["property_id"] == old_property))

    await _save(db, conn)
    return RedirectResponse("/admin?probe=1#dashboard", status_code=303)


@router.post("/property", dependencies=[Depends(require_admin_owner)])
async def select_property(body: PropertyPick, db: AsyncSession = Depends(get_db)) -> dict:
    conn = await _get(db)
    if not conn.get("refresh_token"):
        raise HTTPException(status_code=400, detail="Connect GA4 first.")
    props = await _list_properties(await _access_token(conn["refresh_token"]))
    picked = next((x for x in props if x["property_id"] == body.property_id), None)
    if not picked:
        raise HTTPException(status_code=400, detail="That GA4 property is not accessible.")
    conn.update(picked)
    conn["choices"] = props
    conn["last_error"] = ""
    await _save(db, conn)
    return _safe(conn)


@router.post("/disconnect", dependencies=[Depends(require_admin_owner)])
async def disconnect(db: AsyncSession = Depends(get_db)) -> dict:
    conn = await _get(db)
    refresh = conn.get("refresh_token")
    if refresh:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post("https://oauth2.googleapis.com/revoke", data={"token": refresh})
        except Exception:
            log.warning("ga4_revoke_failed")
    await app_settings.set_value(db, "ga4_connection", {})
    return {"connected": False}
