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

app.include_router(webhook_router)
app.include_router(orders_router)
app.include_router(admin_router)
app.include_router(agent_admin_router)

# Static assets for the dashboard (CSS/JS — no secrets, safe to serve openly)
from pathlib import Path

from fastapi.staticfiles import StaticFiles

app.mount(
    "/admin/static",
    StaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="static",
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
