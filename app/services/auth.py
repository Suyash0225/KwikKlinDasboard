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
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import Cookie, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    ROLE_ACCOUNTANT,
    ROLE_MANAGER,
    ROLE_OWNER,
    WRITABLE_STATUSES,
    LoginSession,
    Tenant,
    User,
)

log = structlog.get_logger()

SESSION_COOKIE = "kk_session"
SESSION_DAYS = 30

# scrypt parameters — ~100ms per hash on a normal machine. Login ke liye
# theek, brute force ke liye mehnga.
_N, _R, _P = 2**14, 8, 1

# per-IP login throttle: 8 galat try / 10 min
_FAILED: dict[str, deque] = defaultdict(deque)
_FAIL_WINDOW = 600
_FAIL_MAX = 8


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
        dk = hashlib.scrypt(
            (password or "").encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)),
        )
        return hmac.compare_digest(dk, bytes.fromhex(hash_hex))
    except Exception:
        return False


def temp_password() -> str:
    """Pehli baar bhejne wala password — bolne/type karne layak."""
    return "kk-" + secrets.token_urlsafe(6).replace("_", "").replace("-", "")[:8]


# --- throttle ---------------------------------------------------------------


def throttled(ip: str) -> bool:
    q = _FAILED[ip]
    now = time.monotonic()
    while q and now - q[0] > _FAIL_WINDOW:
        q.popleft()
    return len(q) >= _FAIL_MAX


def note_failure(ip: str) -> None:
    _FAILED[ip].append(time.monotonic())


def clear_failures(ip: str) -> None:
    _FAILED.pop(ip, None)


# --- sessions ---------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


async def start_session(
    db: AsyncSession, user: User, *, ip: str = "", user_agent: str = ""
) -> str:
    """Naya session banao; client ko RAW token milta hai (DB mein hash)."""
    token = secrets.token_urlsafe(32)
    db.add(
        LoginSession(
            user_id=user.id,
            token_hash=_token_hash(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
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


async def require_write(p: Principal = Depends(current_user)) -> Principal:
    """Data badalne wale endpoints ke liye. Paisa ruka ho to 402."""
    if not p.can_write:
        raise HTTPException(
            status_code=402,
            detail="Subscription band hai — data dikhega par badla nahi ja sakta. "
                   "Payment karke wapas chalu karein.",
        )
    if p.role == ROLE_ACCOUNTANT:
        raise HTTPException(status_code=403, detail="Accountant sirf dekh sakta hai")
    return p


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
