"""Dukaan ke bahar ke API connections — Instagram aur Google Business.

Token/credential sirf VENDOR Control panel se jodte, badalte, hatate hain
(routers/control.py). Owner dashboard sirf haal dekhta hai — koi token
browser tak nahi jaata, na wahan se likha ja sakta hai. WhatsApp ka yahi
niyam tenants.wa_* par pehle se hai.

Instagram token settings_kv mein ENCRYPTED rehta hai (secrets.py, wahi
TOKEN_ENCRYPTION_KEY). Purani plaintext value bhi padhi jaati hai aur
scripts/encrypt_tokens.py use ek baar mein encrypt kar deta hai.
"""

from contextlib import asynccontextmanager

import httpx

from app.database import async_session_factory
from app.services import app_settings, secrets, tenant_context

GRAPH = "https://graph.facebook.com/v21.0"


class IntegrationError(ValueError):
    pass


@asynccontextmanager
async def tenant_db(tid):
    """Us dukaan ke context mein NAYA session — RLS ka app.tenant_id
    transaction shuru hote hi lagta hai, isliye context pehle, session baad."""
    async with tenant_context.as_tenant(tid):
        async with async_session_factory() as db:
            yield db


# ------------------------------------------------------------- instagram --


async def instagram_creds(db) -> tuple[str, str]:
    """(ig_user_id, token) — token decrypted. Khali = juda nahi."""
    user = (await app_settings.get(db, "ig_user_id") or "").strip()
    token = secrets.decrypt((await app_settings.get(db, "ig_access_token") or "").strip()) or ""
    return user, token


async def instagram_status(db) -> dict:
    user, token = await instagram_creds(db)
    return {"linked": bool(user and token), "user_id": user}


async def check_instagram(user_id: str, token: str) -> str:
    """Graph par live jaanch — account ka username, warna IntegrationError."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{GRAPH}/{user_id}", params={"fields": "username", "access_token": token})
    except httpx.HTTPError:
        raise IntegrationError("Could not reach Meta — try again")
    if r.status_code != 200:
        raise IntegrationError("Meta rejected this Instagram user ID / token")
    return r.json().get("username") or ""


async def set_instagram(db, user_id: str, token: str) -> str:
    user_id, token = user_id.strip(), token.strip()
    if not user_id.isdigit() or len(token) < 20:
        raise IntegrationError("Instagram user ID must be numeric and the token looks too short")
    username = await check_instagram(user_id, token)
    await app_settings.set_value(db, "ig_user_id", user_id)
    await app_settings.set_value(db, "ig_access_token", secrets.encrypt(token))
    return username


async def clear_instagram(db) -> None:
    await app_settings.set_value(db, "ig_user_id", "")
    await app_settings.set_value(db, "ig_access_token", "")
