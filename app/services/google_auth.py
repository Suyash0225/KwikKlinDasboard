"""Google se login — OAuth 2.0 authorization-code flow.

Rasta:

    /api/auth/google/start     -> Google ke consent screen par bhej do
    Google                     -> user "allow" karta hai
    /api/auth/google/callback  -> code aata hai; hum SEEDHE Google se
                                  token maangte hain, aur id_token se
                                  email/naam nikaalte hain

**Signature verify kyun nahi kar rahe:** id_token humein browser se nahi,
Google ke token endpoint se **server-to-server HTTPS** par milta hai. Google
ke apne docs kehte hain ki us soorat mein signature verify karna zaroori
nahi — TLS + client_secret ne pehle hi sabit kar diya ki ye Google hai.
Phir bhi `aud` (humara client id) aur `iss` (Google) dono check karte hain,
aur `email_verified` ke bina kisi ko andar nahi aane dete.

Agar kabhi id_token BROWSER se lene lago (Google Sign-In JS button), to ye
baat palat jaati hai — tab signature verify karna PADEGA.
"""

import base64
import json
import secrets
from urllib.parse import urlencode

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# CSRF: state cookie. Callback par cookie aur query dono match hone chahiye.
STATE_COOKIE = "kk_oauth_state"
# signup ke beech Google ki pehchaan yahan rakhi jaati hai (short-lived)
PENDING_COOKIE = "kk_google_pending"


class GoogleAuthError(Exception):
    """Google se login nahi ho paya. Caller isse user-friendly message banata."""


def enabled() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


async def public_base(db) -> str:
    """Bahar se app kis URL par dikhti hai.

    Laptop se chalte waqt ye cloudflare tunnel hota hai, jiska URL har
    restart par badal jaata hai — tunnel_guard use `public_base_url` setting
    mein likhta rehta hai. APP_BASE_URL (127.0.0.1) sirf fallback hai, aur
    usse Google ka callback phone par kabhi wapas nahi aa sakta.
    """
    from app.services import app_settings

    try:
        live = (await app_settings.get(db, "public_base_url") or "").strip()
    except Exception:
        live = ""
    return (live or settings.APP_BASE_URL).rstrip("/")


def redirect_uri(base: str | None = None) -> str:
    base = (base or settings.APP_BASE_URL).rstrip("/")
    return base + "/api/auth/google/callback"


def start_url(state: str, base: str | None = None) -> str:
    """Consent screen ka URL."""
    return AUTH_URL + "?" + urlencode(
        {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": redirect_uri(base),
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            # har baar account chunne do — ek laptop par do dukaan ho sakti hain
            "prompt": "select_account",
            "access_type": "online",
        }
    )


def new_state() -> str:
    return secrets.token_urlsafe(24)


def _b64url_json(segment: str) -> dict:
    pad = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + pad).decode())


async def exchange_code(code: str, base: str | None = None) -> dict:
    """code -> Google ki verified pehchaan.

    `base` wahi hona chahiye jo start_url() ko diya tha — Google dono baar
    ek jaisa redirect_uri maangta hai, warna wo code reject kar deta hai.

    Returns {"sub", "email", "name", "picture"}. Raises GoogleAuthError.
    """
    if not enabled():
        raise GoogleAuthError("Google login is server par configure nahi hai")

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": redirect_uri(base),
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError as exc:
        raise GoogleAuthError(f"Google se baat nahi ho payi: {exc}") from exc

    if r.status_code != 200:
        log.warning("google_token_exchange_failed", status=r.status_code, body=r.text[:200])
        raise GoogleAuthError("Google ne login confirm nahi kiya")

    id_token = (r.json() or {}).get("id_token")
    if not id_token or id_token.count(".") != 2:
        raise GoogleAuthError("Google se pehchaan nahi mili")

    try:
        claims = _b64url_json(id_token.split(".")[1])
    except Exception as exc:
        raise GoogleAuthError("Google ka jawab padha nahi gaya") from exc

    # Ye token humare hi app ke liye bana hai?
    aud = claims.get("aud")
    if aud != settings.GOOGLE_CLIENT_ID:
        log.warning("google_aud_mismatch", aud=str(aud)[:40])
        raise GoogleAuthError("Ye login humare app ke liye nahi tha")
    if claims.get("iss") not in _ISSUERS:
        log.warning("google_iss_mismatch", iss=str(claims.get("iss"))[:40])
        raise GoogleAuthError("Google ki pehchaan sahi nahi lagi")

    email = (claims.get("email") or "").strip().lower()
    # Bina verify kiye email par account dena = koi bhi kisi ka account le le
    if not email or not claims.get("email_verified"):
        raise GoogleAuthError("Aapka Google email verified nahi hai")

    ident = {
        "sub": str(claims.get("sub") or ""),
        "email": email,
        "name": (claims.get("name") or email.split("@")[0]).strip()[:120],
        "picture": claims.get("picture") or "",
    }
    log.info("google_identity_ok", email=email)
    return ident
