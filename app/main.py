"""FastAPI application entry point.

Run locally:
    .venv\\Scripts\\uvicorn.exe app.main:app --reload
"""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.routers.admin import router as admin_router
from app.routers.agent_admin import router as agent_admin_router
from app.routers.orders import router as orders_router
from app.routers.webhook import router as webhook_router
from app.utils.logger import configure_logging

configure_logging()
log = structlog.get_logger()


async def _multi_tenant_hardening_check() -> None:
    """60 dukaanon se pehle jo pakka hona chahiye — nahi hai to ERROR log."""
    from sqlalchemy import func, select

    from app.database import async_session_factory
    from app.models.tenant import WRITABLE_STATUSES, Tenant

    async with async_session_factory() as db:
        n = (
            await db.execute(
                select(func.count()).select_from(Tenant).where(Tenant.status.in_(WRITABLE_STATUSES))
            )
        ).scalar_one()
    if n <= 1:
        return
    gaps: list[str] = []
    if not settings.TOKEN_ENCRYPTION_KEY:
        gaps.append("TOKEN_ENCRYPTION_KEY empty — WhatsApp tokens stored in plaintext")
    if not settings.VENDOR_API_KEY:
        gaps.append("VENDOR_API_KEY empty — shop ADMIN_API_KEY doubles as vendor master key")
    elif settings.VENDOR_API_KEY == settings.ADMIN_API_KEY:
        gaps.append("VENDOR_API_KEY equals ADMIN_API_KEY — one leak opens both")
    if settings.ADMIN_API_KEY.startswith("change-me"):
        gaps.append("ADMIN_API_KEY is the .env.example placeholder")
    if gaps:
        log.error("multi_tenant_hardening_incomplete", active_tenants=n, gaps=gaps)
    else:
        log.info("multi_tenant_hardening_ok", active_tenants=n)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("startup", shop=settings.SHOP_NAME, environment=settings.ENVIRONMENT)
    from app.services import scheduler, tenant_context

    # Prime the home-tenant cache before any traffic: the sync DB events
    # (insert stamping) can only read the cache, never resolve it themselves.
    try:
        await tenant_context.get_home_tenant_id()
    except Exception:
        log.exception("home_tenant_prime_failed")

    # Multi-tenant hardening check: ek se zyada dukaan aur secrets/keys
    # single-shop wale hi hain to shuru mein hi chilla do. Rokta nahi —
    # existing deploy chalta rahe — par log mein ERROR, har restart par.
    try:
        await _multi_tenant_hardening_check()
    except Exception:
        log.exception("hardening_check_failed")

    scheduler.start()
    # owner's edited message formats survive restarts
    try:
        from app.database import async_session_factory
        from app.services import app_settings as _as
        from app.services.messages import load_overrides

        async with async_session_factory() as db:
            load_overrides(await _as.get(db, "message_overrides"))
    except Exception:
        log.exception("message_overrides_load_failed")
    yield
    scheduler.shutdown()
    await engine.dispose()
    log.info("shutdown")


app = FastAPI(
    title="Laundry WhatsApp Bot",
    version="0.1.0",
    lifespan=lifespan,
    # No public API docs in production — this service faces the internet
    # (Meta webhooks) and its API surface is nobody else's business.
    docs_url="/docs" if settings.ENVIRONMENT == "development" else None,
    redoc_url=None,
)

from fastapi import Request
from fastapi.middleware.gzip import GZipMiddleware

# Big JSON payloads (inbox threads, reports) shrink ~10x over the tunnel.
app.add_middleware(GZipMiddleware, minimum_size=1024)


class _noop:
    """Tenant na mile (staff panel par bina cookie) to context waisa hi —
    None, yaani system; RLS kuch nahi dikhata, jo sahi hai."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@app.middleware("http")
async def tenant_scope(request: Request, call_next):
    """Data isolation, layer 1: har HTTP request par tenant context set karo.

    - Valid kk_session cookie -> us user ka tenant.
    - Warna (anonymous, API key, webhook) -> is instance ka HOME tenant.

    Aage kya hota hai: app/database.py isi context se har transaction par
    Postgres ko `SET LOCAL app.tenant_id` bhejta hai (RLS enforce karta hai),
    har ORM SELECT par tenant filter lagta hai, aur har naya row isi tenant
    par stamp hota hai. HTTP se aane wali koi query kabhi unscoped nahi
    chal sakti — chahe endpoint code filter karna bhool bhi jaye.
    """
    from app.services import tenant_context

    path = request.url.path
    if path.startswith("/admin/static/") or path == "/health":
        return await call_next(request)  # no tenant data behind these

    # Platform surfaces — kisi ek dukaan ke nahi, sabki: vendor control
    # panel (key-gated) aur Razorpay ka webhook (jis dukaan ka paisa aaya,
    # wo notes se nikalti hai). Ye SYSTEM context mein chalte hain, home
    # mein nahi — warna invoices/billing_events par RLS lagte hi control
    # panel ko sirf home dikhta aur doosri dukaan ka invoice insert hi na
    # hota (WITH CHECK). Rate limit ke liye home ki id hi key rehti hai.
    if path.startswith("/control") or path == "/webhooks/razorpay":
        home = await tenant_context.get_home_tenant_id()
        if home is not None and _rate_limited(home, path):
            return JSONResponse(
                status_code=429,
                content={"detail": "Bahut tezi se requests — thoda ruk kar try karein."},
                headers={"Retry-After": "30"},
            )
        async with _noop():
            return await call_next(request)

    tid = None
    # Staff panel ka apna rasta: tenant us aadmi ke TOKEN se aata hai.
    # Login se pehle (token nahi hai) context system rehta hai — us waqt
    # hum jaante hi nahi ki kis dukaan ka aadmi hai, aur home tenant maan
    # lena galat hoga: doosri dukaan ka staff kabhi login hi na kar paata.
    if path.startswith("/staff"):
        staff_token = request.cookies.get("kk_staff", "")
        tid = (
            await tenant_context.tenant_id_for_staff_token(staff_token)
            if staff_token
            else None
        )
        async with tenant_context.as_tenant(tid) if tid is not None else _noop():
            return await call_next(request)

    token = request.cookies.get("kk_session", "")
    if token:
        tid = await tenant_context.tenant_id_for_session(token)
    if tid is None:
        tid = await tenant_context.get_home_tenant_id()

    # Per-tenant rate limit: ek client ka runaway loop baaki sab tenants ko
    # slow nahi kar sakta. Sirf API paths par; webhook (Meta ka traffic,
    # burst aata hai) aur static exempt hain.
    if tid is not None and _rate_limited(tid, request.url.path):
        return JSONResponse(
            status_code=429,
            content={"detail": "Bahut tezi se requests — thoda ruk kar try karein."},
            headers={"Retry-After": "30"},
        )

    # Owner ka number bhi context mein — "malik ko batao" wali har jagah
    # isi tenant ke malik ko bole, .env wale ko nahi.
    async with tenant_context.as_tenant(tid) if tid is not None else _noop():
        return await call_next(request)


# Sliding window per tenant (in-memory — restart par reset, theek hai).
from collections import defaultdict as _dd, deque as _deque
import time as _time

_RL_BUCKETS: dict = _dd(lambda: _deque(maxlen=4096))
_RL_PREFIXES = ("/admin/api", "/admin/media", "/orders", "/api/", "/control", "/staff/api")


def _rate_limited(tid, path: str) -> bool:
    if not path.startswith(_RL_PREFIXES):
        return False
    limit = settings.RATE_LIMIT_PER_MIN
    if limit <= 0:
        return False  # 0/negative = disabled
    now = _time.monotonic()
    dq = _RL_BUCKETS[tid]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= limit:
        log.warning("tenant_rate_limited", tenant=str(tid), path=path)
        return True
    dq.append(now)
    return False


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline hardening headers on every response.

    Referrer-Policy matters most here: media URLs carry ?key=..., and this
    stops that key from leaking via the Referer header.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
    )
    return response


app.include_router(webhook_router)
app.include_router(orders_router)
app.include_router(admin_router)
app.include_router(agent_admin_router)

# Bechne ka rasta: pricing -> signup -> payment -> login (app/routers/account.py)
from app.routers.account import router as account_router

app.include_router(account_router)

# Suyash ka apna panel: sab clients, plans, paisa (app/routers/control.py)
from app.routers.control import router as control_router

app.include_router(control_router)

# Dukaan ke aadmi ka apna panel — owner ke dashboard se bilkul alag rasta,
# alag cookie, alag pehre (app/routers/staff_panel.py)
from app.routers.staff_panel import router as staff_panel_router

app.include_router(staff_panel_router)

# Static assets for the dashboard (CSS/JS — no secrets, safe to serve openly)
from pathlib import Path

from fastapi.staticfiles import StaticFiles


class CachedStaticFiles(StaticFiles):
    """StaticFiles + real cache headers.

    CSS/JS are referenced with ?v=<mtime> (the dashboard route rewrites the
    version on every deploy), so the browser may cache them forever — a
    repeat visit re-downloads nothing. HTML must always revalidate, or a
    stale shell would point at assets that no longer exist.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        path = str(args[0] if args else kwargs.get("full_path", ""))
        if path.endswith(".html"):
            resp.headers["Cache-Control"] = "no-cache"
        else:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


app.mount(
    "/admin/static",
    CachedStaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="static",
)


# PWA service worker. Served from /admin/sw.js (not /admin/static/...) with a
# Service-Worker-Allowed header so its scope can cover the whole /admin app.
# no-cache: a deployed SW update must be picked up on the next visit.
@app.get("/admin/sw.js", include_in_schema=False)
async def service_worker():
    from fastapi.responses import Response as _Resp

    sw = Path(__file__).resolve().parent / "static" / "sw.js"
    return _Resp(
        content=sw.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/admin"},
    )


# Public page: pricing + signup + login. Yahi wo darwaza hai jahan se ek
# nayi laundry andar aati hai (app/static/join.html).
_JOIN_FILE = Path(__file__).resolve().parent / "static" / "join.html"


_WELCOME_FILE = Path(__file__).resolve().parent / "static" / "welcome.html"


@app.get("/welcome", include_in_schema=False)
async def welcome_page():
    """Naye client ki landing. Yahan is dukaan ka koi data nahi hai — sirf
    unke apne account ki halat aur aage ka rasta."""
    from fastapi.responses import Response as _Resp

    return _Resp(
        content=_WELCOME_FILE.read_text(encoding="utf-8"),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", include_in_schema=False)
@app.get("/join", include_in_schema=False)
async def join_page():
    from fastapi.responses import Response as _Resp

    return _Resp(
        content=_JOIN_FILE.read_text(encoding="utf-8"),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


# Super-admin (vendor) panel ka HTML shell. Shell mein zero data hai — har
# API call X-API-Key maangti hai (require_vendor_key), jo page par daalni
# padti hai. Isi liye ye route control router (key-gated) ke BAHAR hai.
_CONTROL_FILE = Path(__file__).resolve().parent / "static" / "control.html"


@app.get("/control", include_in_schema=False)
async def control_page():
    """Vendor panel ka shell.

    STRICT CSP: script/style sirf apne origin se (no 'unsafe-inline') —
    isi liye JS/CSS externalized hain aur markup mein koi inline handler
    ya style attribute nahi. XSS ghus bhi jaye to script chal hi nahi
    sakti, aur vendor session cookie httpOnly hai to padhi bhi nahi jaa
    sakti. Asset links ?v=<mtime> se aate hain, isliye HTML no-store
    rehta hai par JS/CSS immutable cache hote hain.
    """
    from fastapi.responses import Response as _Resp

    html = _CONTROL_FILE.read_text(encoding="utf-8")
    static_dir = _CONTROL_FILE.parent
    v = int(max(
        (static_dir / "control.js").stat().st_mtime,
        (static_dir / "control.css").stat().st_mtime,
    ))
    html = html.replace("__V__", str(v))
    return _Resp(
        content=html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
        },
    )


_BILLING_FILE = Path(__file__).resolve().parent / "static" / "billing.html"


@app.get("/billing", include_in_schema=False)
async def billing_page():
    """Client ka plan/usage/recharge page (data /api/billing/* se, session par)."""
    from fastapi.responses import Response as _Resp

    return _Resp(
        content=_BILLING_FILE.read_text(encoding="utf-8"),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


_STAFF_FILE = Path(__file__).resolve().parent / "static" / "staff.html"


@app.get("/staff", include_in_schema=False)
async def staff_page():
    """Dukaan ke aadmi ka panel. Page khud khula hai — data har call par
    kk_staff cookie se guzarta hai, aur wo cookie sirf /staff par jaati hai.

    CSP yahan bhi sakht: koi inline script nahi, koi bahar ka origin nahi.
    """
    from fastapi.responses import Response as _Resp

    html = _STAFF_FILE.read_text(encoding="utf-8")
    static_dir = _STAFF_FILE.parent
    v = int(max(
        (static_dir / "staff.js").stat().st_mtime,
        (static_dir / "staff.css").stat().st_mtime,
    ))
    import re as _re

    html = _re.sub(r"(staff\.(?:js|css))\?v=[\w]+", rf"\1?v={v}", html)
    return _Resp(
        content=html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; form-action 'none'; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
        },
    )


@app.get("/staff/manifest.webmanifest", include_in_schema=False)
async def staff_manifest():
    """Phone par 'Add to home screen' — isi file se app ki tarah lagta hai."""
    from fastapi.responses import Response as _Resp

    path = Path(__file__).resolve().parent / "static" / "staff-manifest.json"
    return _Resp(
        content=path.read_text(encoding="utf-8"),
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/staff/icon.svg", include_in_schema=False)
async def staff_icon():
    from fastapi.responses import Response as _Resp

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
        '<rect width="192" height="192" rx="42" fill="#111827"/>'
        '<rect x="26" y="26" width="140" height="140" rx="34" fill="#f97316"/>'
        '<text x="96" y="124" font-family="system-ui,sans-serif" font-size="72" '
        'font-weight="800" fill="#fff" text-anchor="middle">KK</text></svg>'
    )
    return _Resp(content=svg, media_type="image/svg+xml",
                 headers={"Cache-Control": "public, max-age=86400"})


@app.get("/staff/sw.js", include_in_schema=False)
async def staff_sw():
    """Service worker — sirf itna ki app install ho sake aur kholte hi
    khul jaye. Kaam ka data JAAN-BOOJH KAR cache nahi hota: purana order
    dikhana kisi ko galat kaam par bhej sakta hai."""
    from fastapi.responses import Response as _Resp

    js = (
        "const SHELL='kkstaff-v2';\n"
        "self.addEventListener('install',e=>{e.waitUntil(caches.open(SHELL)"
        ".then(c=>c.addAll(['/staff','/admin/static/staff.css','/admin/static/staff.js'])))});\n"
        "self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(k=>"
        "Promise.all(k.filter(x=>x!==SHELL).map(x=>caches.delete(x)))))});\n"
        "self.addEventListener('fetch',e=>{\n"
        "  const u=new URL(e.request.url);\n"
        "  if(e.request.method!=='GET'||u.pathname.startsWith('/staff/api')) return;\n"
        "  e.respondWith(fetch(e.request).then(r=>{\n"
        "    const copy=r.clone(); caches.open(SHELL).then(c=>c.put(e.request,copy)); return r;\n"
        "  }).catch(()=>caches.match(e.request)));\n"
        "});\n"
    )
    return _Resp(content=js, media_type="application/javascript",
                 headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/staff"})


@app.get("/social/{name}")
async def social_image(name: str):
    """Public marketing posters (Instagram fetches from here). Only files the
    daily social job created — nothing else in media/ is ever exposed."""
    from pathlib import Path as _P

    from fastapi.responses import FileResponse

    safe = _P(name).name
    if not (safe.startswith("social-") and safe.endswith(".png")):
        return JSONResponse(status_code=404, content={"detail": "not found"})
    path = _P(__file__).resolve().parent / "media" / safe
    if not path.exists():
        return JSONResponse(status_code=404, content={"detail": "not found"})
    return FileResponse(path, media_type="image/png")


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness + DB connectivity check. Never raises: reports instead."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        log.exception("health_check_db_failed")
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "db": "unreachable"},
        )
    return JSONResponse(content={"status": "ok", "db": "connected"})
