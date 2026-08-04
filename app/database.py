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
