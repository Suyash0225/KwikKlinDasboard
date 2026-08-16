"""Request-scoped tenant context — the spine of data isolation.

Kaun kaun is ContextVar ko padhta hai:

1. `app/main.py` ka tenant middleware — HAR HTTP request par set karta hai
   (session ho to us user ka tenant, warna is instance ka HOME tenant).
2. `app/database.py` ka "begin" event — har DB transaction par
   `SET LOCAL app.tenant_id` bhejta hai; Postgres Row-Level Security
   policies isi GUC se enforce karti hain (DB-level isolation).
3. `app/database.py` ke ORM events — har SELECT par automatic
   `tenant_id = <ctx>` filter, har naye row par automatic tenant stamp.

SYSTEM CONTEXT (ContextVar = None): scheduler jobs, webhook replay,
alembic migrations, pg_dump — in-process trusted code jo kisi HTTP user ke
behalf par nahi chal raha. Iske liye RLS policy full access deti hai
(GUC unset => policy pass) aur naye rows HOME tenant par stamp hote hain.
HTTP surface par ye kabhi nahi hota: middleware har request par context set
karta hai, isliye API se aane wali har query hamesha scoped hai.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

import structlog

log = structlog.get_logger()

# None => system context (trusted in-process code, no HTTP user behind it).
current_tenant_id: ContextVar[uuid.UUID | None] = ContextVar(
    "current_tenant_id", default=None
)

# Home tenant cache — is deployment ki apni dukaan. Resolve once, reuse.
# sync DB events (before_flush) await nahi kar sakte, isliye cache zaroori hai.
_home_id: uuid.UUID | None = None


def cached_home_tenant_id() -> uuid.UUID | None:
    """Sync read of the cache — for use inside sync ORM events only."""
    return _home_id


def effective_tenant_id() -> uuid.UUID | None:
    """Jis tenant par naya row stamp hona chahiye — abhi ke context ka,
    warna home. Core-level inserts (jo ORM before_flush ko bypass karte
    hain, e.g. pg_insert ON CONFLICT) iski value explicitly likhein."""
    return current_tenant_id.get() or cached_home_tenant_id()


def invalidate_home_cache() -> None:
    global _home_id
    _home_id = None


async def get_home_tenant_id() -> uuid.UUID | None:
    """Home tenant ka id — cached. Apna session kholta hai (tenants table
    RLS-scoped nahi hai, isliye yahan koi recursion/filter issue nahi)."""
    global _home_id
    if _home_id is not None:
        return _home_id
    from app.database import async_session_factory
    from app.services.auth import home_tenant

    try:
        async with async_session_factory() as db:
            home = await home_tenant(db)
    except Exception:
        log.exception("home_tenant_resolve_failed")
        return None
    if home is not None:
        _home_id = home.id
    return _home_id


async def tenant_id_for_session(token: str) -> uuid.UUID | None:
    """kk_session cookie -> us user ka tenant_id (invalid/expired -> None).

    Apna DB session use karta hai; users/login_sessions tables tenant-scoped
    nahi hain, isliye ye lookup context set hone se pehle bhi safe hai.
    """
    if not token:
        return None
    from app.database import async_session_factory
    from app.services.auth import user_for_token

    try:
        async with async_session_factory() as db:
            user = await user_for_token(db, token)
    except Exception:
        log.exception("tenant_context_session_lookup_failed")
        return None
    return user.tenant_id if user is not None else None


async def tenant_id_for_staff_token(token: str) -> uuid.UUID | None:
    """kk_staff cookie -> us staff ki DUKAAN ka tenant_id.

    Staff panel ka tenant SIRF yahan se aata hai — request body/query se
    kabhi nahi, warna koi bhi doosri dukaan ka id bhej kar uska kaam
    maang leta. Lookup jaan-boojh kar system context mein hai: is waqt
    tak hum jaante hi nahi ki kis tenant ka aadmi hai.
    """
    if not token:
        return None
    import hashlib
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models import StaffSession

    prev = current_tenant_id.set(None)          # system context
    try:
        async with async_session_factory() as db:
            row = (
                await db.execute(
                    select(StaffSession).where(
                        StaffSession.token_hash
                        == hashlib.sha256(token.encode()).hexdigest()
                    )
                )
            ).scalar_one_or_none()
        if row is None or row.revoked_at is not None:
            return None
        if row.expires_at <= datetime.now(timezone.utc):
            return None
        return row.tenant_id
    except Exception:
        log.exception("tenant_context_staff_lookup_failed")
        return None
    finally:
        current_tenant_id.reset(prev)
