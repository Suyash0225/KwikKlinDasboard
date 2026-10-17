"""PostgreSQL advisory-lock leader election for the in-process scheduler.

Every web worker runs this lightweight election loop. Only the worker holding
the session-level advisory lock starts APScheduler. The lock lives on a
dedicated checked-out connection; a process crash or broken connection releases
it in PostgreSQL, allowing a standby worker to take over.
"""

import asyncio
from collections.abc import Callable

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = structlog.get_logger()

# Stable application-specific 64-bit advisory lock key ("KKLN"). Do not change
# this between deployments or workers, or multiple schedulers could run.
SCHEDULER_LOCK_KEY = 0x4B4B4C4E


class SchedulerLeader:
    def __init__(
        self,
        engine: AsyncEngine,
        start_scheduler: Callable[[], None],
        shutdown_scheduler: Callable[[], None],
        *,
        poll_interval: float = 5.0,
        health_interval: float = 5.0,
    ) -> None:
        if poll_interval <= 0 or health_interval <= 0:
            raise ValueError("scheduler election intervals must be positive")
        self._engine = engine
        self._start_scheduler = start_scheduler
        self._shutdown_scheduler = shutdown_scheduler
        self._poll_interval = poll_interval
        self._health_interval = health_interval
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._connection: AsyncConnection | None = None
        self._scheduler_started = False
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def start(self) -> None:
        """Start election in the background; this worker may be a standby."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name="scheduler-leader-election"
        )

    async def stop(self) -> None:
        """Stop jobs, release the advisory lock, and close the held connection."""
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        await task
        self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            conn: AsyncConnection | None = None
            acquired = False
            try:
                conn = await self._engine.connect()
                result = await conn.execute(
                    text(
                        "SELECT pg_try_advisory_lock(CAST(:key AS bigint)), "
                        "pg_backend_pid()"
                    ),
                    {"key": SCHEDULER_LOCK_KEY},
                )
                lock_row = result.one()
                acquired = bool(lock_row[0])
                backend_pid = int(lock_row[1])
                # Advisory locks are session-level, so committing the query
                # does not release the lock; keeping this connection open does.
                await conn.commit()

                if acquired:
                    self._connection = conn
                    self._is_leader = True
                    # Mark before invoking start so a partially failed start
                    # still gets a best-effort shutdown in the finally block.
                    self._scheduler_started = True
                    self._start_scheduler()
                    log.info(
                        "scheduler_leader_acquired",
                        lock_key=SCHEDULER_LOCK_KEY,
                    )
                    await self._monitor_connection(conn, backend_pid)
                else:
                    log.debug(
                        "scheduler_standby",
                        retry_seconds=self._poll_interval,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A lost DB connection releases the session lock server-side.
                # Stop this worker's jobs before trying to become leader again.
                log.exception(
                    "scheduler_leader_loop_failed",
                    was_leader=self._is_leader,
                )
            finally:
                if self._scheduler_started:
                    self._stop_jobs()
                if conn is not None and acquired:
                    await self._unlock(conn)
                if conn is not None:
                    await self._close(conn)
                if self._connection is conn:
                    self._connection = None
                self._is_leader = False

            if self._stop_event.is_set():
                return
            if await self._wait(self._poll_interval):
                return

    async def _monitor_connection(
        self, conn: AsyncConnection, expected_backend_pid: int
    ) -> None:
        """Keep the lock connection healthy; exit on shutdown, raise on loss."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._health_interval
                )
                return
            except asyncio.TimeoutError:
                # A connection failure triggers scheduler shutdown and election
                # retry. Other workers can acquire the lock after this session
                # disappears, providing automatic failover.
                async def _ping() -> int:
                    result = await conn.execute(text("SELECT pg_backend_pid()"))
                    pid = int(result.scalar_one())
                    await conn.commit()
                    return pid

                current_pid = await asyncio.wait_for(
                    _ping(), timeout=self._health_interval
                )
                if current_pid != expected_backend_pid:
                    raise RuntimeError(
                        "scheduler leader connection was replaced; advisory lock "
                        "belongs to the previous PostgreSQL session"
                    )

    async def _wait(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return self._stop_event.is_set()

    def _stop_jobs(self) -> None:
        try:
            self._shutdown_scheduler()
        except Exception:
            log.exception("scheduler_leader_shutdown_failed")
        finally:
            self._scheduler_started = False
            log.info("scheduler_leader_released")

    async def _unlock(self, conn: AsyncConnection) -> None:
        try:
            if conn.in_transaction():
                await conn.rollback()
            result = await conn.execute(
                text("SELECT pg_advisory_unlock(CAST(:key AS bigint))"),
                {"key": SCHEDULER_LOCK_KEY},
            )
            unlocked = bool(result.scalar_one())
            await conn.commit()
            log.info("scheduler_advisory_lock_unlocked", unlocked=unlocked)
        except Exception:
            # A plain close may return a live DB session to the pool with its
            # session-level lock still held. Invalidate it to force server-side
            # disconnect and guarantee that PostgreSQL releases the lock.
            log.warning("scheduler_advisory_unlock_failed", exc_info=True)
            try:
                await conn.invalidate()
            except Exception:
                log.warning("scheduler_leader_invalidate_failed", exc_info=True)

    async def _close(self, conn: AsyncConnection) -> None:
        try:
            await conn.close()
        except Exception:
            log.warning("scheduler_leader_connection_close_failed", exc_info=True)
