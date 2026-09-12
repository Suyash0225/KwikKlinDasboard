"""Secrets at rest — tenants.wa_token jaise columns DB mein encrypted.

Kyun: 60 dukaanon ka WhatsApp token ek table mein plain text = ek DB
dump/backup leak par sab ke WhatsApp par koi bhi kuch bhi bhej de. Fernet
(AES-128-CBC + HMAC, cryptography package) se column encrypt hota hai;
key sirf .env mein (TOKEN_ENCRYPTION_KEY), DB mein kabhi nahi.

Kaise: SQLAlchemy TypeDecorator — model par `EncryptedText` lagao, baaki
code ko pata bhi nahi chalta. Likhte waqt encrypt, padhte waqt decrypt.

Format: "enc:v1:<fernet token>". Prefix isliye ki purani plaintext rows
(is feature se pehle ki) bhi padhi ja sakein — wo waise hi wapas aati hain,
aur agli baar likhne par encrypted ho jaati hain. Ek baar mein sab convert
karne ke liye: scripts/encrypt_tokens.py.

Key khali ho: plaintext hi likha/padha jaata hai (purana single-shop deploy
bina .env badle chalta rahe), par startup par ek WARNING — 2 se zyada
dukaan wale ko ye key set karni hi chahiye.
"""

from __future__ import annotations

import structlog
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

log = structlog.get_logger()

_PREFIX = "enc:v1:"
_fernet = None
_checked = False


def _get_fernet():
    """Lazy — settings import time par cryptography load na ho, aur key
    galat ho to pehle write par saaf error aaye, import par nahi."""
    global _fernet, _checked
    if _checked:
        return _fernet
    _checked = True
    from app.config import settings

    key = (settings.TOKEN_ENCRYPTION_KEY or "").strip()
    if not key:
        log.warning(
            "token_encryption_disabled",
            hint="set TOKEN_ENCRYPTION_KEY in .env — tenants.wa_token is stored in plaintext",
        )
        return None
    from cryptography.fernet import Fernet

    _fernet = Fernet(key.encode())
    return _fernet


def encrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    f = _get_fernet()
    if f is None:
        return value
    if value.startswith(_PREFIX):
        return value  # already encrypted (double-write guard)
    return _PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str | None) -> str | None:
    if value is None or not value.startswith(_PREFIX):
        return value  # legacy plaintext, ya khali
    f = _get_fernet()
    if f is None:
        # Row encrypted hai par key nahi — ye config ki galti hai, chhupao mat.
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY missing but an encrypted value is stored — "
            "restore the key from your .env backup"
        )
    from cryptography.fernet import InvalidToken

    try:
        return f.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY does not match the stored value — wrong key?"
        ) from exc


def is_enabled() -> bool:
    return _get_fernet() is not None


class EncryptedText(TypeDecorator):
    """Text column jo DB mein encrypted rehta hai, Python mein plain."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
