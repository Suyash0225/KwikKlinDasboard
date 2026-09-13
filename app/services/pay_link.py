"""UPI pay-link ke tokens — bina DB ke, sign karke.

Kyun DB row nahi: link ek hi kaam ka hai — "is dukaan ko itna paisa do".
Uske liye ek table banana matlab migration, aur phir purane rows ki safai
ka ek aur kaam. Signed token mein wahi do cheezein andar hi chali jaati
hain, aur expiry token khud carry karta hai.

Token public URL mein jaata hai, isliye do baatein:

1. Ismein sirf tenant id aur amount hai. Na order number, na phone, na
   grahak ka naam — link kisi ke bhi haath lag sakta hai (WhatsApp forward
   ek tap ka kaam hai), aur usse kuch pata nahi chalna chahiye.
2. Signature ke bina koi bhi amount badal kar "₹5 do" wala link bana leta.
   Isliye HMAC, aur padhne se pehle hamesha verify.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from decimal import Decimal

from app.config import settings

# Link kitne din chale. Reminder ka jawab log do-chaar din mein dete hain;
# mahine bhar purana link zinda rakhne ka koi faayda nahi, aur galat amount
# par paisa aa jaana bura hai.
TTL_SECONDS = 14 * 24 * 3600

_SEP = "."


def _key() -> bytes:
    """Signing key — apni, kisi aur kaam ki key udhaar nahi.

    TOKEN_ENCRYPTION_KEY DB ke tokens ke liye hai aur ADMIN_API_KEY auth ke
    liye. Unhi ko yahan bhi laga dena matlab ek key do jagah — ek badalne
    par doosri jagah chup-chaap toot jaati. Isliye derive karke alag.
    """
    base = (settings.TOKEN_ENCRYPTION_KEY or settings.ADMIN_API_KEY or "").encode()
    return hashlib.sha256(b"kk-pay-link:" + base).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make(tenant_id: uuid.UUID, amount: Decimal, now: float | None = None) -> str:
    """Ek link token: kis dukaan ko, kitna, kab tak."""
    exp = int((now if now is not None else time.time()) + TTL_SECONDS)
    body = f"{tenant_id.hex}:{Decimal(amount):.2f}:{exp}"
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).digest()[:12]
    return _b64(body.encode()) + _SEP + _b64(sig)


def parse(token: str, now: float | None = None) -> tuple[uuid.UUID, Decimal] | None:
    """(tenant_id, amount) — ya None agar token galat, chhera hua, ya expired.

    Har fail par None: caller ko 404 dena hai, aur "signature galat hai" bनाम
    "expire ho gaya" batane se sirf chhedne wale ki madad hoti hai.
    """
    try:
        body_b64, sig_b64 = token.split(_SEP)
        body = _unb64(body_b64)
        want = hmac.new(_key(), body, hashlib.sha256).digest()[:12]
        if not hmac.compare_digest(want, _unb64(sig_b64)):
            return None
        tid_hex, amount_s, exp_s = body.decode().split(":")
        if (now if now is not None else time.time()) > int(exp_s):
            return None
        amount = Decimal(amount_s)
        if amount <= 0:
            return None
        return uuid.UUID(hex=tid_hex), amount
    except Exception:
        return None


def upi_uri(vpa: str, payee: str, amount: Decimal) -> str:
    """UPI deep link. Yahi wo cheez hai jo GPay/PhonePe/Paytm kholti hai.

    WhatsApp is scheme ko link nahi banata — isiliye message mein https
    jaata hai aur ye URI us page se fire hota hai.
    """
    from urllib.parse import quote

    return (
        f"upi://pay?pa={quote(vpa, safe='')}"
        f"&pn={quote(payee or 'Shop', safe='')}"
        f"&am={Decimal(amount):.2f}&cu=INR"
    )
