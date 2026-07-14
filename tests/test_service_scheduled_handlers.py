from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import (
    CollectionPolicyVersion,
    CollectionTask,
    KnownVideo,
    ScheduledJob,
    SnapshotCohort,
    SnapshotCohortComponent,
    VideoCollectionState,
    VideoMetricSnapshot,
)
from books_of_time.db.repositories import EventRepository, ScheduledJobRepository
from books_of_time.domain.enums import ScheduledJobKind, TaskKind, TaskStatus
from books_of_time.service.scheduled_jobs import (
    SnapshotCohortPlanningScheduleHandler,
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
            DiscoveryUidSource(
                mid="100",
                pool_type="game",
                pool_id="game-a",
                game_id="game-a",
                official=True,
            ),
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
    assert tasks[0].payload["source_associations"] == [
        {
            "source_mid": "100",
            "pool_type": "game",
            "pool_id": "game-a",
            "game_id": "game-a",
            "official": True,
            "monitored": True,
        },
        {
            "source_mid": "100",
            "pool_type": "matrix",
            "pool_id": "matrix",
            "game_id": None,
            "official": False,
            "monitored": True,
        },
    ]
    assert tasks[1].payload["source_pool_type"] == "event"
    assert tasks[1].payload["source_pool_id"] == "event-a"
    assert all(task.priority == 110 for task in tasks)
    assert all(task.payload["discovery_schedule_mode"] == "normal" for task in tasks)
    assert all(task.payload["focus_time"] is None for task in tasks)
    assert all(task.payload["focus_offset_seconds"] is None for task in tasks)
    assert all(now.isoformat() in (task.idempotency_key or "") for task in tasks)
    await engine.dispose()


@pytest.mark.asyncio
async def test_uid_discovery_handler_skips_slot_at_stop_boundary() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)  # 22:00 Asia/Shanghai
    handler = UidDiscoveryScheduleHandler(
        [DiscoveryUidSource(mid="100", pool_type="matrix")]
    )
    async with session_factory() as session:
        job = await _job(session, kind=ScheduledJobKind.UID_DISCOVERY, now=now)
        await handler.handle(job, session, now=now)
        await session.commit()

    async with session_factory() as session:
        tasks = list(await session.scalars(select(CollectionTask)))

    assert tasks == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_uid_discovery_handler_schedules_focus_slot_and_recheck_30_seconds_later() -> (
    None
):
    engine, session_factory = await _session_factory()
    scheduled_for = datetime(2026, 7, 13, 3, 0, tzinfo=UTC)  # 11:00 Asia/Shanghai
    delayed_now = scheduled_for + timedelta(minutes=2)
    handler = UidDiscoveryScheduleHandler(
        [DiscoveryUidSource(mid="100", pool_type="matrix")]
    )
    async with session_factory() as session:
        job = await _job(
            session,
            kind=ScheduledJobKind.UID_DISCOVERY,
            now=scheduled_for,
        )
        await handler.handle(job, session, now=delayed_now)
        await session.commit()

    async with session_factory() as session:
        tasks = list(
            await session.scalars(
                select(CollectionTask).order_by(CollectionTask.not_before)
            )
        )

    assert len(tasks) == 2
    assert all(task.priority == 120 for task in tasks)
    assert [task.not_before for task in tasks] == [
        delayed_now,
        delayed_now + timedelta(seconds=30),
    ]
    assert [task.payload["discovery_schedule_mode"] for task in tasks] == [
        "focus",
        "focus",
    ]
    assert [task.payload["focus_time"] for task in tasks] == ["11:00", "11:00"]
    assert [task.payload["focus_offset_seconds"] for task in tasks] == [0, 30]
    assert [task.payload["scheduled_for"] for task in tasks] == [
        scheduled_for.isoformat(),
        (scheduled_for + timedelta(seconds=30)).isoformat(),
    ]
    assert [task.payload["scheduler_slot"] for task in tasks] == [
        scheduled_for.isoformat(),
        scheduled_for.isoformat(),
    ]
    assert tasks[0].idempotency_key != tasks[1].idempotency_key
    await engine.dispose()


@pytest.mark.asyncio
async def test_uid_discovery_handler_does_not_repeat_completed_focus_pair() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 13, 3, 0, tzinfo=UTC)  # 11:00 Asia/Shanghai
    handler = UidDiscoveryScheduleHandler(
        [DiscoveryUidSource(mid="100", pool_type="matrix")]
    )
    async with session_factory() as session:
        job = await _job(session, kind=ScheduledJobKind.UID_DISCOVERY, now=now)
        await handler.handle(job, session, now=now)
        tasks = list(await session.scalars(select(CollectionTask)))
        for task in tasks:
            task.status = TaskStatus.SUCCEEDED
        await handler.handle(job, session, now=now + timedelta(seconds=10))
        await session.commit()

    async with session_factory() as session:
        tasks = list(await session.scalars(select(CollectionTask)))

    assert len(tasks) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_uid_discovery_handler_merges_active_event_targets_by_uid() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 7, 0, tzinfo=UTC)
    async with session_factory() as session:
        repository = EventRepository(session)
        first_event = await repository.create_event(
            slug="event-a",
            name="事件 A",
            start_at=now - timedelta(days=1),
            now=now,
        )
        second_event = await repository.create_event(
            slug="event-b",
            name="事件 B",
            now=now,
        )
        expired_event = await repository.create_event(
            slug="expired-event",
            name="已结束事件",
            start_at=now - timedelta(days=2),
            end_at=now - timedelta(days=1),
            now=now,
        )
        first_target = await repository.add_target(
            event_id=first_event.id,
            target_type="uid",
            target_value="100",
            extra={"role": "official"},
            now=now,
        )
        second_target = await repository.add_target(
            event_id=second_event.id,
            target_type="uid",
            target_value="100",
            extra={"role": "major_creator"},
            now=now,
        )
        await repository.add_target(
            event_id=expired_event.id,
            target_type="uid",
            target_value="300",
            now=now,
        )
        job = await _job(session, kind=ScheduledJobKind.UID_DISCOVERY, now=now)
        handler = UidDiscoveryScheduleHandler(
            [DiscoveryUidSource(mid="100", pool_type="matrix")]
        )
        await handler.handle(job, session, now=now)
        await session.commit()

    async with session_factory() as session:
        tasks = list(await session.scalars(select(CollectionTask)))

    assert len(tasks) == 1
    assert tasks[0].target_id == "100"
    assert tasks[0].payload["source_pool_type"] == "event"
    assert tasks[0].payload["source_pool_id"] == f"target:{first_target.id}"
    assert tasks[0].payload["source_associations"] == [
        {
            "source_mid": "100",
            "pool_type": "event",
            "pool_id": f"target:{first_target.id}",
            "game_id": None,
            "official": True,
            "monitored": True,
        },
        {
            "source_mid": "100",
            "pool_type": "event",
            "pool_id": f"target:{second_target.id}",
            "game_id": None,
            "official": False,
            "monitored": True,
        },
        {
            "source_mid": "100",
            "pool_type": "matrix",
            "pool_id": "matrix",
            "game_id": None,
            "official": False,
            "monitored": True,
        },
    ]
    assert tasks[0].payload["event_links"] == [
        {"event_id": first_event.id, "target_id": first_target.id},
        {"event_id": second_event.id, "target_id": second_target.id},
    ]
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
            "scheduler": {
                "discovery_scan_seconds": 45,
                "discovery_start_hour": 9,
                "discovery_stop_hour": 21,
                "discovery_timezone": "Asia/Shanghai",
                "discovery_focus_times": ["10:30", "20:00"],
            },
            "discovery": {"matrix_uids": []},
        }
    )

    assert {definition.job_kind for definition in definitions} == {
        ScheduledJobKind.UID_DISCOVERY,
        ScheduledJobKind.VIDEO_SNAPSHOT_SWEEP,
        ScheduledJobKind.DAILY_TERMINAL_SNAPSHOT,
        ScheduledJobKind.OPERATIONAL_ALERT_EVALUATION,
    }
    uid_definition = next(
        definition
        for definition in definitions
        if definition.job_kind == ScheduledJobKind.UID_DISCOVERY
    )
    assert uid_definition.schedule_seconds == 45
    assert set(handlers) == {definition.job_kind for definition in definitions}
    uid_handler = handlers[ScheduledJobKind.UID_DISCOVERY]
    assert isinstance(uid_handler, UidDiscoveryScheduleHandler)
    assert uid_handler.policy.start_hour == 9
    assert uid_handler.policy.stop_hour == 21
    assert uid_handler.policy.timezone_name == "Asia/Shanghai"
    assert uid_handler.policy.focus_times == ("10:30", "20:00")


def test_default_scheduled_jobs_add_optional_shadow_cohort_planner() -> None:
    definitions, handlers = build_default_scheduled_jobs(
        {
            "discovery": {"matrix_uids": []},
            "snapshot_cohorts": {
                "enabled": True,
                "policy_version": "cohort-default-v1",
                "rollout_mode": "shadow",
                "planning_seconds": 45,
            },
        }
    )

    definition = next(
        item
        for item in definitions
        if item.job_kind is ScheduledJobKind.SNAPSHOT_COHORT_PLANNING
    )
    assert definition.job_key == "snapshot-cohort-planning"
    assert definition.schedule_seconds == 45
    assert definition.priority == 110
    assert isinstance(
        handlers[ScheduledJobKind.SNAPSHOT_COHORT_PLANNING],
        SnapshotCohortPlanningScheduleHandler,
    )


def test_default_scheduled_jobs_reject_live_cohort_rollout_until_c7() -> None:
    with pytest.raises(
        ValueError,
        match=(
            r"snapshot_cohorts\.rollout_mode=live is unavailable until C7 migrates "
            "all routine scheduling ownership"
        ),
    ):
        build_default_scheduled_jobs(
            {
                "discovery": {"matrix_uids": []},
                "snapshot_cohorts": {
                    "enabled": True,
                    "rollout_mode": "live",
                },
            }
        )


@pytest.mark.asyncio
async def test_shadow_cohort_handler_persists_evidence_without_collection_tasks() -> (
    None
):
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
    definitions, handlers = build_default_scheduled_jobs(
        {
            "discovery": {"matrix_uids": []},
            "snapshot_cohorts": {
                "enabled": True,
                "rollout_mode": "shadow",
            },
        }
    )
    definition = next(
        item
        for item in definitions
        if item.job_kind is ScheduledJobKind.SNAPSHOT_COHORT_PLANNING
    )
    handler = handlers[ScheduledJobKind.SNAPSHOT_COHORT_PLANNING]

    async with session_factory() as session:
        session.add(
            KnownVideo(
                bvid="BV-SERVICE-SHADOW",
                source_mid="42",
                pubdate=now - timedelta(hours=1),
                first_seen_at=now - timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )
        job = await ScheduledJobRepository(session).ensure(
            job_key=definition.job_key,
            job_kind=definition.job_kind,
            schedule_seconds=definition.schedule_seconds,
            priority=definition.priority,
            payload=definition.payload,
            next_run_at=now,
        )
        await handler.handle(job, session, now=now)
        await session.commit()

    async with session_factory() as session:
        policy = await session.scalar(select(CollectionPolicyVersion))
        state = await session.get(VideoCollectionState, "BV-SERVICE-SHADOW")
        cohort = await session.scalar(select(SnapshotCohort))
        components = list(await session.scalars(select(SnapshotCohortComponent)))
        tasks = list(await session.scalars(select(CollectionTask)))

        assert policy is not None and policy.active is True
        assert state is not None
        assert cohort is not None and cohort.status == "shadow_planned"
        assert len(components) == 3
        assert tasks == []

    await engine.dispose()


@pytest.mark.parametrize("discovery_scan_seconds", [0, 61])
def test_default_scheduled_jobs_reject_discovery_interval_that_can_miss_focus_minute(
    discovery_scan_seconds: int,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"scheduler\.discovery_scan_seconds must be between 1 and 60",
    ):
        build_default_scheduled_jobs(
            {
                "scheduler": {
                    "discovery_scan_seconds": discovery_scan_seconds,
                },
                "discovery": {"matrix_uids": []},
            }
        )
