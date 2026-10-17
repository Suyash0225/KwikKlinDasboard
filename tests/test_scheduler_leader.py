"""PostgreSQL advisory-lock election and failover tests."""

import asyncio

from app.database import engine
from app.services.scheduler_leader import SchedulerLeader


class FakeScheduler:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.started_event = asyncio.Event()
        self.stopped_event = asyncio.Event()

    def start(self) -> None:
        self.started += 1
        self.started_event.set()

    def shutdown(self) -> None:
        self.stopped += 1
        self.stopped_event.set()


async def test_one_worker_leads_and_standby_takes_over_after_connection_loss() -> None:
    first_scheduler = FakeScheduler()
    second_scheduler = FakeScheduler()
    first = SchedulerLeader(
        engine,
        first_scheduler.start,
        first_scheduler.shutdown,
        poll_interval=0.05,
        health_interval=0.1,
    )
    second = SchedulerLeader(
        engine,
        second_scheduler.start,
        second_scheduler.shutdown,
        # A standby should poll slower than the leader's health check so the
        # disconnected leader shuts down before takeover begins.
        poll_interval=0.5,
        health_interval=0.05,
    )

    try:
        await first.start()
        await asyncio.wait_for(first_scheduler.started_event.wait(), timeout=8)

        await second.start()
        await asyncio.sleep(0.15)
        assert first_scheduler.started == 1
        assert second_scheduler.started == 0

        # Simulate the leader process losing its PostgreSQL session. The
        # session-level advisory lock is released by PostgreSQL on disconnect.
        connection = first._connection
        assert connection is not None
        await connection.invalidate()

        await asyncio.wait_for(first_scheduler.stopped_event.wait(), timeout=5)
        await asyncio.wait_for(second_scheduler.started_event.wait(), timeout=5)
        assert first_scheduler.stopped >= 1
        assert second_scheduler.started == 1
    finally:
        await first.stop()
        await second.stop()

    assert first_scheduler.stopped >= 1
    assert second_scheduler.stopped == 1
