"""Async database engine, session factory, and the FastAPI dependency.

Usage in a router:

    from app.database import get_db

    @router.get("/things")
    async def list_things(db: AsyncSession = Depends(get_db)):
        ...

The dependency hands out a session and rolls back on any unhandled error.
Committing is the caller's job — services decide transaction boundaries.
"""

import logging
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

logger = logging.getLogger(__name__)

# pool_pre_ping: test connections before handing them out, so a Postgres
# restart doesn't surface as a mid-request crash.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    # 10 steady + 20 burst connections: enough for a busy inbox plus the
    # scheduler's background sessions without exhausting local Postgres
    # (default max_connections=100).
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    # recycle before Windows/router NAT idle-kills a connection silently
    pool_recycle=1800,
)

# expire_on_commit=False: objects stay usable after commit — without this,
# every attribute access after commit triggers a surprise lazy refresh
# (which raises in async code).
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session, rolls back on unhandled errors."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            logger.exception("db session rollback due to unhandled error")
            await session.rollback()
            raise


# --- Tenant isolation wiring (see app/services/tenant_context.py) -----------
#
# Three enforcement points, layered — koi ek chhoot jaye to baaki pakadte hain:
#   1. "begin" event  -> SET LOCAL app.tenant_id  -> Postgres RLS (DB level)
#   2. do_orm_execute -> automatic tenant filter on every ORM SELECT
#   3. before_flush   -> automatic tenant stamp on every new row
#
# Context None (scheduler/webhook-replay/migrations) = system context:
# no RLS restriction, no SELECT filter, new rows stamped with the HOME tenant.

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from app.services import tenant_context


@event.listens_for(engine.sync_engine, "begin")
def _rls_set_tenant(conn) -> None:
    """Har transaction ke shuru mein Postgres ko batao kis tenant ki request
    hai. SET LOCAL transaction-scoped hai — commit/rollback par khud reset,
    isliye pooled connections ke beech kabhi leak nahi hota."""
    tid = tenant_context.current_tenant_id.get()
    if tid is not None:
        # tid is a uuid.UUID (never raw user input) — safe to inline.
        conn.exec_driver_sql(f"SET LOCAL app.tenant_id = '{tid}'")


@event.listens_for(Session, "do_orm_execute")
def _orm_tenant_filter(execute_state) -> None:
    """Automatic WHERE tenant_id = <ctx> on every ORM SELECT of a
    TenantScoped model — endpoints ko yaad rakhne ki zaroorat hi nahi."""
    tid = tenant_context.current_tenant_id.get()
    if tid is None:
        return  # system context — scheduler/webhook replay see everything
    if (
        execute_state.is_select
        and not execute_state.is_column_load
        and not execute_state.is_relationship_load
    ):
        from app.models.base import TenantScoped

        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                TenantScoped,
                lambda cls: cls.tenant_id == tid,
                include_aliases=True,
            )
        )


@event.listens_for(Session, "before_flush")
def _stamp_tenant_on_new_rows(session, flush_context, instances) -> None:
    """Naye TenantScoped rows par tenant_id likho:
    HTTP context -> us request ka tenant; system context -> HOME tenant.
    Explicitly set kiya hua tenant_id kabhi overwrite nahi hota (RLS
    WITH CHECK galat value ko DB level par pakad legi)."""
    from app.models.base import TenantScoped

    tid = tenant_context.current_tenant_id.get()
    if tid is None:
        tid = tenant_context.cached_home_tenant_id()
    if tid is None:
        # Home cache abhi tak resolve nahi hua (startup se pehle ki flush?).
        # Row NULL-tenant chala jayega — RLS system context mein dikhega,
        # tenant context mein nahi. Loud log, kyunki ye kabhi normal nahi hai.
        for obj in session.new:
            if isinstance(obj, TenantScoped):
                logger.warning(
                    "tenant_stamp_skipped_no_home_cache: %s", type(obj).__name__
                )
        return
    for obj in session.new:
        if isinstance(obj, TenantScoped) and getattr(obj, "tenant_id", None) is None:
            obj.tenant_id = tid
