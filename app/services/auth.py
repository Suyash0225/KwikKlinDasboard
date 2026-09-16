"""Asli login: passwords, sessions, roles.

Ye `X-API-Key` ki jagah leta hai. Wo ek shop ke liye theek tha — bechne wale
software mein ek hi chaabi sabke paas nahi ho sakti.

Design:
- **scrypt** (stdlib `hashlib`) — koi nayi dependency nahi, aur ye ek asli
  password KDF hai (sha256 nahi, jo passwords ke liye galat hai).
- Session ka **token client ke paas jaata hai, hash DB mein** — leaked
  database se koi login nahi kar sakta.
- Cookie **HttpOnly + SameSite=Lax** — JS chura nahi sakta, CSRF se bachaav.
- Har galat login **per-IP throttle** ke andar — brute force chalega hi nahi.
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import Cookie, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    ROLE_MANAGER,
    ROLE_OWNER,
    WRITABLE_STATUSES,
    LoginSession,
    Tenant,
    User,
)
from app.utils.throttle import IPThrottle

log = structlog.get_logger()

SESSION_COOKIE = "kk_session"
SESSION_DAYS = 30

# scrypt parameters — ~100ms per hash on a normal machine. Login ke liye
# theek, brute force ke liye mehnga.
_N, _R, _P = 2**14, 8, 1

# scrypt ke wo parameters jo hum SACH MEIN jaari karte hain. verify_password
# n/r/p stored hash ki string se padhta hai, isliye wo attacker-shaped ho
# sakte hain (corrupted row, purana import, DB write). Bina is list ke
# n=2**30 wali ek row login attempt ko memory bomb bana deti thi.
# Params kabhi badlein to naya tuple YAHAN jodein aur purana rakhein,
# warna purane users login nahi kar payenge.
_ALLOWED_SCRYPT_PARAMS = {(_N, _R, _P)}
# hash ki lambai bhi stored string se aati hai (dklen). 32 hum likhte hain;
# range rakhi hai taaki kal 64 par jaayen to kuch na toote.
_MIN_DKLEN, _MAX_DKLEN = 16, 64

# per-IP login throttle: 8 galat try / 10 min
_FAIL_WINDOW = 600
_FAIL_MAX = 8
_login_throttle = IPThrottle(
    max_failures=_FAIL_MAX, window_secs=_FAIL_WINDOW, name="login"
)


# --- passwords --------------------------------------------------------------


def hash_password(password: str) -> str:
    """'scrypt$n$r$p$salt$hash' — salt har password ka apna."""
    if not password or len(password) < 8:
        raise ValueError("password kam se kam 8 characters ka hona chahiye")
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. Kharab/purana hash -> False, exception nahi."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        params = (int(n), int(r), int(p))
        # Ye teen number DB ki string se aa rahe hain, humare code se nahi.
        # scrypt ki memory ~ 128*n*r bytes hai: n=2**30 wali ek row par ek
        # login attempt poora process kha jaata. Sirf apne jaari kiye hue
        # params par kaam karo — baaki kuch bhi ho to bas "galat password".
        if params not in _ALLOWED_SCRYPT_PARAMS:
            log.warning("password_hash_unknown_params", params=params)
            return False
        expected = bytes.fromhex(hash_hex)
        if not _MIN_DKLEN <= len(expected) <= _MAX_DKLEN:
            return False
        dk = hashlib.scrypt(
            (password or "").encode(), salt=bytes.fromhex(salt_hex),
            n=params[0], r=params[1], p=params[2], dklen=len(expected),
        )
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# Confusable characters jaan-boojh kar bahar: 0/O, 1/l/I. Ye password
# WhatsApp par jaata hai aur banda ise phone par haath se type karta hai.
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # 31 chars


def temp_password() -> str:
    """Pehli baar bhejne wala password — bolne/type karne layak.

    Pehle ye `token_urlsafe(6)` se `-` aur `_` hata kar `[:8]` karta tha.
    Do dikkatein: entropy ~36 bits reh jaati thi, aur lambai TAY nahi thi —
    jis token mein do-teen special char aa gaye, uska password chhota ho
    jaata tha. Ab 10 chars x 31-char alphabet = ~49 bits, aur lambai hamesha
    ek jaisi (13 with prefix), yaani `hash_password()` ka 8-char minimum
    kabhi miss nahi hota.
    """
    return "kk-" + "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(10))


# --- throttle ---------------------------------------------------------------


def throttled(ip: str) -> bool:
    return _login_throttle.throttled(ip)


def note_failure(ip: str) -> None:
    _login_throttle.note_failure(ip)


def clear_failures(ip: str) -> None:
    _login_throttle.clear(ip)


# --- sessions ---------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


async def start_session(
    db: AsyncSession, user: User, *, ip: str = "", user_agent: str = "",
    minutes: int | None = None,
) -> str:
    """Naya session banao; client ko RAW token milta hai (DB mein hash).

    minutes: chhota TTL (impersonation jaise cases) — default 30 din."""
    token = secrets.token_urlsafe(32)
    db.add(
        LoginSession(
            user_id=user.id,
            token_hash=_token_hash(token),
            expires_at=datetime.now(timezone.utc) + (
                timedelta(minutes=minutes) if minutes else timedelta(days=SESSION_DAYS)
            ),
            ip=ip[:64] or None,
            user_agent=user_agent[:200] or None,
        )
    )
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    log.info("login_ok", user=user.email, role=user.role)
    return token


async def end_session(db: AsyncSession, token: str) -> None:
    row = (
        await db.execute(
            select(LoginSession).where(LoginSession.token_hash == _token_hash(token))
        )
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()


async def end_all_sessions(db: AsyncSession, user_id) -> int:
    """Password badla / laptop kho gaya — sab jagah se logout."""
    rows = (
        await db.execute(select(LoginSession).where(LoginSession.user_id == user_id))
    ).scalars().all()
    for r in rows:
        await db.delete(r)
    await db.commit()
    return len(rows)


async def user_for_token(db: AsyncSession, token: str) -> User | None:
    """Token -> User, ya None (expired/unknown). Expired row saaf ho jaati hai."""
    if not token:
        return None
    row = (
        await db.execute(
            select(LoginSession).where(LoginSession.token_hash == _token_hash(token))
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at <= datetime.now(timezone.utc):
        await db.delete(row)
        await db.commit()
        return None
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    return user


# --- FastAPI dependencies ---------------------------------------------------


async def home_tenant(db: AsyncSession) -> Tenant | None:
    """Ye deployment KIS dukaan ka hai.

    Ek instance ek hi shop ka data rakhta hai (ARCHITECTURE.md). Naye signup
    se bane tenants ka data yahan hai hi nahi — isliye unhe is dashboard tak
    pahunchne dena poori tarah galat hai. `home_tenant_slug` setting ise tay
    karti hai; khali ho to sabse purana tenant home maana jaata hai.
    """
    from app.services import app_settings

    try:
        slug = (await app_settings.get(db, "home_tenant_slug") or "").strip()
    except Exception:
        slug = ""
    if slug:
        t = (
            await db.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if t is not None:
            return t
    return (
        await db.execute(select(Tenant).order_by(Tenant.created_at))
    ).scalars().first()


async def is_home_user(db: AsyncSession, user: User | None) -> bool:
    """Kya ye user ISI dukaan ka hai (yaani ise yahan ka data dikh sakta hai)?"""
    if user is None:
        return False
    home = await home_tenant(db)
    if home is None:
        return False
    return user.tenant_id == home.id


class Principal:
    """Request ke peeche kaun hai — user + uska tenant."""

    def __init__(self, user: User, tenant: Tenant | None):
        self.user = user
        self.tenant = tenant

    @property
    def role(self) -> str:
        return self.user.role

    @property
    def can_write(self) -> bool:
        """read_only tenant (paisa ruka) mein koi likh nahi sakta."""
        if self.tenant is None:
            return True
        return self.tenant.status in WRITABLE_STATUSES


async def current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    kk_session: str = Cookie(default=""),
) -> Principal:
    """Logged-in user chahiye. Nahi hai -> 401."""
    user = await user_for_token(db, kk_session)
    if user is None:
        raise HTTPException(status_code=401, detail="Login karein")
    tenant = await db.get(Tenant, user.tenant_id) if user.tenant_id else None
    if tenant is not None and tenant.status in ("suspended", "cancelled"):
        raise HTTPException(
            status_code=403,
            detail="Ye account band hai. Support se baat karein.",
        )
    request.state.principal = Principal(user, tenant)
    return request.state.principal


def require_role(*roles: str):
    """`Depends(require_role(ROLE_OWNER))` — role ke bina 403."""

    async def _dep(p: Principal = Depends(current_user)) -> Principal:
        if p.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Iske liye {'/'.join(roles)} hona zaroori hai",
            )
        return p

    return _dep



# Owner/Manager hi settings + staff chhoo sakte hain
require_owner = require_role(ROLE_OWNER)
require_manager = require_role(ROLE_OWNER, ROLE_MANAGER)


def require_role_write(*roles: str):
    """Role BHI chahiye aur likhne ka haq bhi.

    Sirf role check karna kaafi nahi tha: read-only (paisa ruka hua) tenant
    ka owner phir bhi naye user bana leta tha.
    """

    async def _dep(p: Principal = Depends(current_user)) -> Principal:
        if p.role not in roles:
            raise HTTPException(
                status_code=403, detail=f"Iske liye {'/'.join(roles)} hona zaroori hai"
            )
        if not p.can_write:
            raise HTTPException(
                status_code=402,
                detail="Subscription band hai — data dikhega par badla nahi ja sakta. "
                       "Payment karke wapas chalu karein.",
            )
        return p

    return _dep


require_owner_write = require_role_write(ROLE_OWNER)
require_manager_write = require_role_write(ROLE_OWNER, ROLE_MANAGER)
