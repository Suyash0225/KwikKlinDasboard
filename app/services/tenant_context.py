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
from contextlib import asynccontextmanager
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


# ---------------------------------------------------------------------------
# Tenant ka owner number + scheduler ke liye per-tenant loop
# ---------------------------------------------------------------------------
#
# Pehle har "owner ko batao" wali jagah settings.MANAGER_PHONE padhti thi —
# yaani .env wali EK dukaan ka number. Ek dukaan tak theek tha; 60 vendor
# mein vendor B ka daily summary vendor A ke malik ko jaata. Ab number
# context se aata hai: HTTP request par middleware set karta hai, scheduler
# har tenant ke liye as_tenant() ke andar chalta hai. Context na ho (purana
# single-shop rasta, tests) to .env wala hi milta hai — kuch tootta nahi.

current_owner_phone: ContextVar[str | None] = ContextVar(
    "current_owner_phone", default=None
)


def manager_phone() -> str:
    """Abhi ke tenant ka owner number; context na ho to .env MANAGER_PHONE."""
    from app.config import settings

    return current_owner_phone.get() or settings.MANAGER_PHONE


# tenant_id -> owner_phone. Har request par tenants table mat maaro; 5 min
# ka cache kaafi hai (owner number saal mein ek baar badalta hai).
_OWNER_TTL_S = 300
_owner_cache: dict[uuid.UUID, tuple[str, float]] = {}


def invalidate_owner_cache(tid: uuid.UUID | None = None) -> None:
    if tid is None:
        _owner_cache.clear()
    else:
        _owner_cache.pop(tid, None)


async def owner_phone_for(tid: uuid.UUID | None) -> str | None:
    """Tenant ka owner_phone — cached. tenants table RLS-scoped nahi hai,
    isliye system context se bhi padh sakte hain."""
    import time

    if tid is None:
        return None
    hit = _owner_cache.get(tid)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    from app.database import async_session_factory
    from app.models.tenant import Tenant

    try:
        async with async_session_factory() as db:
            t = await db.get(Tenant, tid)
    except Exception:
        log.exception("owner_phone_lookup_failed", tenant=str(tid))
        return None
    phone = _valid_phone(t.owner_phone if t else None)
    if phone:
        _owner_cache[tid] = (phone, time.monotonic() + _OWNER_TTL_S)
    return phone


def _valid_phone(raw: str | None) -> str | None:
    """E.164 ya None. Placeholder (+910000000000, bootstrap ka default) ya
    kachra number par None — tab manager_phone() .env wale par gir jaata
    hai, aur normalize_phone() call-site par kabhi nahi phat-ta."""
    from app.utils.phone import normalize_phone

    if not raw or not str(raw).strip():
        return None
    try:
        return normalize_phone(str(raw))
    except ValueError:
        return None


class as_tenant:
    """`async with as_tenant(tid):` — is block ke andar sab kuch us tenant
    ka hai: ORM filter, RLS GUC, naye rows ka stamp, send_message ke creds,
    manager_phone(). Bahar nikalte hi context wapas jaisa tha.

    Scheduler isi se har dukaan ka kaam alag-alag chalata hai. HTTP par
    middleware yahi kaam karta hai; ye uska scheduler-side jud.wa hai.
    """

    def __init__(self, tid: uuid.UUID, owner_phone: str | None = None):
        self.tid = tid
        self.owner_phone = owner_phone
        self._t1 = None
        self._t2 = None

    async def __aenter__(self):
        phone = self.owner_phone or await owner_phone_for(self.tid)
        self._t1 = current_tenant_id.set(self.tid)
        self._t2 = current_owner_phone.set(phone)
        return self

    async def __aexit__(self, *exc):
        current_owner_phone.reset(self._t2)
        current_tenant_id.reset(self._t1)
        return False


async def active_tenants() -> list[tuple[uuid.UUID, str, str]]:
    """(id, slug, owner_phone) un dukaanon ka jinke liye proactive kaam
    (reminders, standup, summary) chalna chahiye — trial ya active. Band
    (locked/suspended/cancelled) dukaan ke customers ko kuch nahi jaata.
    System context mein chalta hai — tenants table RLS ke bahar hai."""
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.tenant import WRITABLE_STATUSES, Tenant

    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Tenant.id, Tenant.slug, Tenant.owner_phone)
                .where(Tenant.status.in_(WRITABLE_STATUSES))
                .order_by(Tenant.created_at)
            )
        ).all()
    return [(r[0], r[1], _valid_phone(r[2]) or "") for r in rows]


def claim_prefix() -> str:
    """sent_events ki key global hai (tenant_id nahi) — do dukaanon ka
    'daysum:2026-09-12' takraye nahi, isliye tenant ka chhota hash aage."""
    tid = current_tenant_id.get()
    return f"t{tid.hex[:8]}:" if tid else ""

@asynccontextmanager
async def system_context(db):
    """Is block mein kisi ek dukaan ka pehra nahi — poora platform.

    Account lifecycle ke kuch kaam kisi dukaan ke ANDAR nahi hote:

      signup        nayi dukaan BANATA hai
      login         user milne se PEHLE pata hi nahi kis dukaan ka hai
      google login  wahi baat, google_sub se
      invite accept token kis dukaan ka hai ye token hi batata hai
      impersonate   vendor kisi bhi dukaan mein ja sakta hai

    Bina session ke in requests ka context HOME tenant hota hai
    (main.py ka tenant_scope). Users/invites par RLS lagte hi ye sab
    tootte hain — doosri dukaan ka owner apne hi account se login nahi kar
    paata, 401, bina kisi wajah ke.

    DO cheezein set karni padti hain aur dono zaroori hain:

      ContextVar  -> ORM ka with_loader_criteria filter
      SET LOCAL   -> Postgres ki RLS policy

    SET LOCAL isliye ki GUC transaction ke shuru mein ContextVar se bheja
    jaata hai (database.py ka "begin" event); beech mein ContextVar badalne
    par wo dobara nahi bhejta. SET LOCAL commit/rollback par khud hat jaata
    hai, isliye pooled connection par leak nahi hota.
    """
    from sqlalchemy import text as _text

    token = current_tenant_id.set(None)
    try:
        await db.execute(_text("SET LOCAL app.tenant_id = ''"))
        yield
    finally:
        current_tenant_id.reset(token)
