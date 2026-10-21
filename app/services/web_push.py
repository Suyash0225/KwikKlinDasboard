"""Staff ke phone par notification (Web Push) — app band ho tab bhi.

SSE (services/events.py) sirf khule hue app tak pahunchta hai. Delivery
wala phone jeb mein rakh kar sadak par hai — naya pickup ya owner ka jawab
use tab tak nahi dikhta jab tak wo khud app na khole. Web Push wo kami
bharta hai: browser ka apna push service (FCM/Apple/Mozilla) phone ko
jagata hai aur service worker notification dikhata hai.

Kuch baatein jaan-boojh kar:
  - pywebpush nahi. Wo cryptography>=47 maangta hai (hum 46 par pinned
    hain) aur aiohttp + requests saath laata hai. Encryption http_ece se
    (RFC 8291, aes128gcm), VAPID py_vapid se, bhejna httpx se — jo pehle
    se hai.
  - VAPID chaabi har dukaan ki apni, settings_kv mein encrypted. Pehle
    subscribe par banti hai; .env mein kuch set nahi karna padta.
  - Kabhi raise nahi karta. Push ek suvidha hai; uski wajah se koi task,
    paisa ya status nahi girna chahiye.
  - Message mein grahak ka data nahi — sirf ishara ("Naya kaam: KK-12").
    Notification lock screen par dikhta hai.
"""

import base64
import json
import os
import time
from urllib.parse import urlsplit

import httpx
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

PUSH_TTL = 24 * 3600          # phone band ho to push service itni der sambhale
SEND_TIMEOUT = 10.0

try:  # chhoti libraries — na hon to push band, baaki app chalta rahe
    import http_ece
    from py_vapid import Vapid02
    AVAILABLE = True
except ImportError:  # pragma: no cover - deployment without the deps
    AVAILABLE = False


# Sirf asli push services. Endpoint phone se aata hai aur server us par POST
# karta hai — bina is list ke koi bhi URL dekar server se manmaani jagah
# request bhijwa sakta tha (SSRF).
PUSH_HOSTS = (
    "fcm.googleapis.com", "android.googleapis.com",
    "updates.push.services.mozilla.com", "push.services.mozilla.com",
    ".push.apple.com", ".notify.windows.com",
)


def allowed_endpoint(endpoint: str) -> bool:
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return False
    return any(host == h.lstrip(".") or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


async def _vapid(db: AsyncSession):
    """Is dukaan ki VAPID chaabi — na ho to banao aur rakho."""
    from app.services import app_settings

    pem = (await app_settings.get(db, "webpush_vapid_private") or "").strip()
    if not pem:
        v = Vapid02()
        v.generate_keys()
        pem = v.private_pem().decode()
        await app_settings.set_value(db, "webpush_vapid_private", pem)
    return Vapid02.from_pem(pem.encode())


async def public_key(db: AsyncSession) -> str | None:
    """Browser ko `applicationServerKey` ke liye (base64url, uncompressed point)."""
    if not AVAILABLE:
        return None
    v = await _vapid(db)
    raw = v.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64(raw)


def _encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """RFC 8291 aes128gcm — har message ke liye nayi sender chaabi aur salt."""
    sender = ec.generate_private_key(ec.SECP256R1())
    return http_ece.encrypt(
        payload, salt=os.urandom(16), private_key=sender,
        dh=_unb64(p256dh), auth_secret=_unb64(auth), version="aes128gcm",
    )


def _vapid_headers(v, endpoint: str, origin: str) -> dict:
    parts = urlsplit(endpoint)
    # "sub" push service ke liye sampark: https URL chalta hai. Dukaan ki site
    # https par na ho (laptop par dev) to bhi kuch to bhejna hai.
    sub = origin if origin.startswith("https://") else "mailto:push@localhost"
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "sub": sub, "exp": int(time.time()) + 12 * 3600}
    return v.sign(claims)


async def _post(endpoint: str, body: bytes, headers: dict) -> int:
    async with httpx.AsyncClient(timeout=SEND_TIMEOUT) as client:
        r = await client.post(endpoint, content=body, headers=headers)
        return r.status_code


async def _prepare(db: AsyncSession, subs, payload: bytes) -> list[tuple]:
    """Har phone ke liye (id, endpoint, headers, encrypted body) — DB yahin, HTTP baad mein."""
    v = await _vapid(db)
    out = []
    for s in subs:
        if not allowed_endpoint(s.endpoint):
            out.append((s.id, None, None, None))
            continue
        headers = {
            **_vapid_headers(v, s.endpoint, s.origin),
            "TTL": str(PUSH_TTL), "Urgency": "high",
            "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        }
        out.append((s.id, s.endpoint, headers, _encrypt(payload, s.p256dh, s.auth)))
    return out


async def _deliver(prepared: list[tuple], tenant_id) -> int:
    """Sirf HTTP — koi DB nahi, isliye request ke saath-saath chal sakta hai.
    Jo phone ab nahi rahe (404/410) unhe apne session mein hatata hai."""
    ok, gone = 0, []
    for sub_id, endpoint, headers, body in prepared:
        if endpoint is None:
            gone.append(sub_id)
            continue
        try:
            status = await _post(endpoint, body, headers)
        except Exception:
            log.warning("web_push_send_error", exc_info=True)
            continue
        if status in (404, 410):          # phone ne ijazat wapas le li / app hata di
            gone.append(sub_id)
        elif 200 <= status < 300:
            ok += 1
        else:
            log.warning("web_push_rejected", status=status)
    if gone:
        from app.models import PushSubscription
        from app.services import background

        async def _forget(bg: AsyncSession) -> None:
            await bg.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone)))
            await bg.commit()

        background.run_after(_forget, tenant_id=tenant_id, label="web_push_forget")
    return ok


async def notify(
    db: AsyncSession, tenant_id, *, staff_ids=None, roles=None,
    title: str, body: str, url: str = "/staff", tag: str = "",
) -> int:
    """In logon (ya in roles) ke phone par notification. Kitne phone, wo lautao.

    Subscriptions padhna, chaabi aur encryption caller ke session par —
    chhota kaam, aur commit ke BAAD bulao. Push service tak HTTP background
    mein: request kabhi push service ka intezaar nahi karti. Kabhi raise nahi.
    """
    from app.models import PushSubscription, Staff

    if not AVAILABLE:
        return 0
    try:
        q = select(PushSubscription)
        ids = [i for i in (staff_ids or []) if i]
        if ids:
            q = q.where(PushSubscription.staff_id.in_(ids))
        elif roles:
            q = q.join(Staff, Staff.id == PushSubscription.staff_id).where(
                Staff.is_active, Staff.role.in_(list(roles))
            )
        else:
            return 0
        subs = (await db.execute(q)).scalars().all()
        if not subs:
            return 0
        payload = json.dumps(
            {"title": title[:80], "body": body[:200], "url": url, "tag": tag[:60]}
        ).encode()
        prepared = await _prepare(db, subs, payload)
    except Exception:
        log.exception("web_push_prepare_failed")
        return 0
    from app.services import background

    background.spawn(_deliver(prepared, tenant_id), label="web_push")
    return len(prepared)
