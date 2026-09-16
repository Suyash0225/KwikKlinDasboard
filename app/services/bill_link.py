"""Grahak ke bill ka web link — /b/<token>: bill dikhe, UPI se payment ho.

WhatsApp par bill ka lamba text padhna mushkil hai aur usme "pay" ka koi
button nahi hota. Link kholte hi saaf bill page, aur baaki paisa ho to
GPay / PhonePe / Paytm ka ek-tap button — amount bhara hua, paisa seedha
dukaan ke UPI (Settings -> Business Profile) mein.

Token: dukaan + order ki pehchaan, HMAC se signed (pay_link jaisa, alag
key). Order number URL mein nahi — number badal kar kisi aur ka bill
dekhna namumkin. Page par amount hamesha LIVE (token ka nahi): payment ke
baad wahi link "Paid" dikhata hai.

Link forward ho sakta hai, isliye page par grahak ka poora phone number
nahi (masked), aur search engines ke liye noindex.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from decimal import Decimal
from urllib.parse import quote

from app.config import settings

# Bill mahino baad bhi dekha jaata hai (dhulai ka hisaab, shikayat).
TTL_SECONDS = 365 * 24 * 3600
_SEP = "."


def _key() -> bytes:
    base = (settings.TOKEN_ENCRYPTION_KEY or settings.ADMIN_API_KEY or "").encode()
    return hashlib.sha256(b"kk-bill-link:" + base).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make(tenant_id: uuid.UUID, order_id: uuid.UUID, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + TTL_SECONDS)
    body = tenant_id.bytes + order_id.bytes + exp.to_bytes(5, "big")
    sig = hmac.new(_key(), body, hashlib.sha256).digest()[:12]
    return _b64(body) + _SEP + _b64(sig)


def parse(token: str, now: float | None = None) -> tuple[uuid.UUID, uuid.UUID] | None:
    """(tenant_id, order_id) — ya None (galat, chhera hua, expired)."""
    try:
        body_b64, sig_b64 = token.split(_SEP)
        body = _unb64(body_b64)
        if len(body) != 37:
            return None
        want = hmac.new(_key(), body, hashlib.sha256).digest()[:12]
        if not hmac.compare_digest(want, _unb64(sig_b64)):
            return None
        if (now if now is not None else time.time()) > int.from_bytes(body[32:], "big"):
            return None
        return uuid.UUID(bytes=body[:16]), uuid.UUID(bytes=body[16:32])
    except Exception:
        return None


def public_base(app_settings_dict: dict | None = None) -> str:
    """Link ka domain: SITE_URL (production), warna dukaan ki public_base_url."""
    if settings.SITE_URL:
        return settings.SITE_URL.rstrip("/")
    return str((app_settings_dict or {}).get("public_base_url") or "").strip().rstrip("/")


def url(order, app_settings_dict: dict | None = None) -> str:
    """Poora link, ya "" jab public domain pata hi nahi (local dev)."""
    base = public_base(app_settings_dict)
    if not base or order.tenant_id is None:
        return ""
    return f"{base}/b/{make(order.tenant_id, order.id)}"


async def url_for(db, order) -> str:
    """Message bhejte waqt: us dukaan ki settings se link (context set hona chahiye)."""
    from app.services.google_auth import public_base

    return url(order, {"public_base_url": await public_base(db)})


def message_line(link: str) -> str:
    """Grahak ke message ke aakhir ki line — link na ho to kuch nahi."""
    return f"\n🧾 View bill & pay online: {link}" if link else ""


def upi_params(vpa: str, payee: str, amount: Decimal, note: str) -> str:
    return (
        f"pa={quote(vpa, safe='@')}&pn={quote(payee or 'Shop', safe='')}"
        f"&am={Decimal(amount):.2f}&cu=INR&tn={quote(note[:60], safe='')}"
    )


# Android: intent:// se wahi app khulta hai (na ho to Play Store ki jagah
# browser fallback). iOS: app ki apni scheme. Desktop: QR scan.
UPI_APPS = (
    ("gpay", "Google Pay", "com.google.android.apps.nbu.paisa.user", "gpay://upi/pay?"),
    ("phonepe", "PhonePe", "com.phonepe.app", "phonepe://pay?"),
    ("paytm", "Paytm", "net.one97.paytm", "paytmmp://pay?"),
)


def app_links(params: str) -> dict:
    out = {"upi": f"upi://pay?{params}"}
    for key, _, pkg, ios in UPI_APPS:
        out[key] = {
            "android": f"intent://pay?{params}#Intent;scheme=upi;package={pkg};end",
            "ios": f"{ios}{params}",
        }
    return out


def qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    svg = img.to_string(encoding="unicode")
    return svg[svg.index("<svg"):]   # xml declaration hatao — HTML mein inline
