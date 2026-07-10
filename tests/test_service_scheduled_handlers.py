from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import (
    CollectionTask,
    KnownVideo,
    ScheduledJob,
    VideoMetricSnapshot,
)
from books_of_time.db.repositories import ScheduledJobRepository
from books_of_time.domain.enums import ScheduledJobKind, TaskKind, TaskStatus
from books_of_time.service.scheduled_jobs import (
    TerminalSnapshotScheduleHandler,
    UidDiscoveryScheduleHandler,
    VideoSnapshotSweepScheduleHandler,
    build_default_scheduled_jobs,
)
from books_of_time.task_orchestrator.discovery_loop import DiscoveryUidSource


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _job(session, *, kind: ScheduledJobKind, now: datetime) -> ScheduledJob:
    return await ScheduledJobRepository(session).ensure(
        job_key=f"test-{kind.value}",
        job_kind=kind,
        schedule_seconds=60,
        priority=100,
        payload={},
        next_run_at=now,
    )


@pytest.mark.asyncio
async def test_uid_discovery_handler_enqueues_each_source_once_per_slot() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 7, 0, tzinfo=UTC)
    handler = UidDiscoveryScheduleHandler(
        [
            DiscoveryUidSource(mid="100", pool_type="matrix"),
            DiscoveryUidSource(mid="200", pool_type="event", pool_id="event-a"),
        ]
    )
    async with session_factory() as session:
        job = await _job(session, kind=ScheduledJobKind.UID_DISCOVERY, now=now)
        await handler.handle(job, session, now=now)
        await handler.handle(job, session, now=now)
        await session.commit()

    async with session_factory() as session:
        tasks = list(
            await session.scalars(
                select(CollectionTask).order_by(CollectionTask.target_id)
            )
        )
    assert len(tasks) == 2
    assert all(task.kind == TaskKind.DISCOVER_USER_VIDEOS for task in tasks)
    assert [task.target_id for task in tasks] == ["100", "200"]
    assert tasks[1].payload["source_pool_type"] == "event"
    assert tasks[1].payload["source_pool_id"] == "event-a"
    assert all(now.isoformat() in (task.idempotency_key or "") for task in tasks)
    await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_handler_schedules_without_uid_sources() -> None:
    engine, session_factory = await _session_factory()
    terminal_at = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
    async with session_factory() as session:
        session.add(
            KnownVideo(
                bvid="BV-TERMINAL",
                source_mid="100",
                pubdate=terminal_at - timedelta(hours=1),
                first_seen_at=terminal_at - timedelta(minutes=30),
            )
        )
        job = await _job(
            session,
            kind=ScheduledJobKind.DAILY_TERMINAL_SNAPSHOT,
            now=terminal_at,
        )
        await TerminalSnapshotScheduleHandler().handle(
            job,
            session,
            now=terminal_at,
        )
        await session.commit()

        task = await session.scalar(select(CollectionTask))
        assert task is not None
        task.status = TaskStatus.SUCCEEDED
        job.next_run_at = terminal_at + timedelta(minutes=1)
        await TerminalSnapshotScheduleHandler().handle(
            job,
            session,
            now=terminal_at + timedelta(minutes=1),
        )
        await session.commit()

    async with session_factory() as session:
        tasks = list(await session.scalars(select(CollectionTask)))
    assert len(tasks) == 1
    assert tasks[0].target_id == "BV-TERMINAL"
    assert tasks[0].payload["reason"] == "daily_terminal_snapshot"
    await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_sweep_enqueues_only_due_video() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 10, 5, tzinfo=UTC)
    published_at = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
    async with session_factory() as session:
        for bvid in ("BV-DUE", "BV-FUTURE"):
            session.add(
                KnownVideo(
                    bvid=bvid,
                    source_mid="100",
                    pubdate=published_at,
                    first_seen_at=published_at,
                )
            )
        session.add_all(
            [
                VideoMetricSnapshot(
                    bvid="BV-DUE",
                    captured_at=now - timedelta(minutes=10),
                    view_count=100,
                ),
                VideoMetricSnapshot(
                    bvid="BV-FUTURE",
                    captured_at=now,
                    view_count=100,
                ),
            ]
        )
        job = await _job(
            session,
            kind=ScheduledJobKind.VIDEO_SNAPSHOT_SWEEP,
            now=now,
        )
        await VideoSnapshotSweepScheduleHandler().handle(
            job,
            session,
            now=now,
        )
        await session.commit()

    async with session_factory() as session:
        tasks = list(await session.scalars(select(CollectionTask)))
    assert [task.target_id for task in tasks] == ["BV-DUE"]
    assert tasks[0].payload["reason"] == "snapshot_sweep"
    await engine.dispose()


def test_default_scheduled_jobs_include_independent_terminal_job() -> None:
    definitions, handlers = build_default_scheduled_jobs(
        {
            "scheduler": {"discovery_scan_seconds": 45},
            "discovery": {"matrix_uids": []},
        }
    )

    assert {definition.job_kind for definition in definitions} == {
        ScheduledJobKind.UID_DISCOVERY,
        ScheduledJobKind.VIDEO_SNAPSHOT_SWEEP,
        ScheduledJobKind.DAILY_TERMINAL_SNAPSHOT,
    }
    uid_definition = next(
        definition
        for definition in definitions
        if definition.job_kind == ScheduledJobKind.UID_DISCOVERY
    )
    assert uid_definition.schedule_seconds == 45
    assert set(handlers) == {definition.job_kind for definition in definitions}
