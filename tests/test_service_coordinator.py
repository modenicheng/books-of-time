from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import CollectionTask, ScheduledJob
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import ScheduledJobKind, TaskKind
from books_of_time.service.coordinator import (
    ScheduledJobCoordinator,
    ScheduledJobDefinition,
)


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class RecordingHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []

    async def handle(self, job, session, *, now: datetime) -> None:
        self.calls.append((job.job_key, now))


class PartialFailureHandler:
    async def handle(self, job, session, *, now: datetime) -> None:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BV-PARTIAL",
            priority=10,
            payload={},
            not_before=now,
        )
        raise RuntimeError("handler failed after partial write")


def _definition(
    kind: ScheduledJobKind = ScheduledJobKind.UID_DISCOVERY,
) -> ScheduledJobDefinition:
    return ScheduledJobDefinition(
        job_key="test-job",
        job_kind=kind,
        schedule_seconds=60,
        priority=100,
        payload={"test": True},
    )


@pytest.mark.asyncio
async def test_coordinator_bootstrap_is_idempotent() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)
    coordinator = ScheduledJobCoordinator(
        session_factory=session_factory,
        definitions=[_definition()],
        handlers={},
        lease_owner="coordinator",
    )

    await coordinator.bootstrap(now=now)
    await coordinator.bootstrap(now=now + timedelta(minutes=1))

    async with session_factory() as session:
        count = await session.scalar(select(func.count(ScheduledJob.id)))
        job = await session.scalar(select(ScheduledJob))
    assert count == 1
    assert job is not None
    assert job.next_run_at == now
    await engine.dispose()


@pytest.mark.asyncio
async def test_coordinator_executes_handler_and_advances_job() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)
    handler = RecordingHandler()
    coordinator = ScheduledJobCoordinator(
        session_factory=session_factory,
        definitions=[_definition()],
        handlers={ScheduledJobKind.UID_DISCOVERY: handler},
        lease_owner="coordinator",
    )
    await coordinator.bootstrap(now=now)

    executed = await coordinator.run_once(now=now)

    async with session_factory() as session:
        job = await session.scalar(select(ScheduledJob))
    assert executed is True
    assert handler.calls == [("test-job", now)]
    assert job is not None
    assert job.next_run_at == now + timedelta(minutes=1)
    assert job.last_succeeded_at == now
    await engine.dispose()


@pytest.mark.asyncio
async def test_coordinator_rolls_back_partial_handler_writes_and_records_failure() -> (
    None
):
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)
    coordinator = ScheduledJobCoordinator(
        session_factory=session_factory,
        definitions=[_definition()],
        handlers={ScheduledJobKind.UID_DISCOVERY: PartialFailureHandler()},
        lease_owner="coordinator",
        retry_delay_seconds=15,
    )
    await coordinator.bootstrap(now=now)

    executed = await coordinator.run_once(now=now)

    async with session_factory() as session:
        job = await session.scalar(select(ScheduledJob))
        tasks = list(await session.scalars(select(CollectionTask)))
    assert executed is True
    assert tasks == []
    assert job is not None
    assert job.consecutive_failures == 1
    assert job.last_error_type == "RuntimeError"
    assert job.next_run_at == now + timedelta(seconds=15)
    await engine.dispose()


@pytest.mark.asyncio
async def test_coordinator_records_unknown_job_handler_without_crashing() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 5, 0, tzinfo=UTC)
    coordinator = ScheduledJobCoordinator(
        session_factory=session_factory,
        definitions=[_definition(ScheduledJobKind.DAILY_TERMINAL_SNAPSHOT)],
        handlers={},
        lease_owner="coordinator",
    )
    await coordinator.bootstrap(now=now)

    assert await coordinator.run_once(now=now) is True

    async with session_factory() as session:
        job = await session.scalar(select(ScheduledJob))
    assert job is not None
    assert job.last_error_type == "LookupError"
    await engine.dispose()


@pytest.mark.asyncio
async def test_coordinator_loop_honors_pre_set_stop_event() -> None:
    engine, session_factory = await _session_factory()
    handler = RecordingHandler()
    stop_event = asyncio.Event()
    stop_event.set()
    coordinator = ScheduledJobCoordinator(
        session_factory=session_factory,
        definitions=[_definition()],
        handlers={ScheduledJobKind.UID_DISCOVERY: handler},
        lease_owner="coordinator",
    )

    executed = await coordinator.run_loop(stop_event=stop_event)

    assert executed == 0
    assert handler.calls == []
    await engine.dispose()
