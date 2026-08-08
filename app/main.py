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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("startup", shop=settings.SHOP_NAME, environment=settings.ENVIRONMENT)
    from app.services import scheduler

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
