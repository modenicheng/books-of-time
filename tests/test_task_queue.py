from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.models import Base, CollectionTask
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind, TaskStatus


@pytest.mark.asyncio
async def test_task_repository_leases_highest_priority_due_task() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = CollectionTaskRepository(session)
        await repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BVLOW",
            priority=10,
            payload={"bvid": "BVLOW"},
            not_before=now - timedelta(seconds=1),
        )
        await repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BVHIGH",
            priority=99,
            payload={"bvid": "BVHIGH"},
            not_before=now - timedelta(seconds=1),
        )
        await session.commit()

        task = await repo.lease_next(
            lease_owner="worker-1",
            now=now,
            lease_seconds=120,
        )

        assert task is not None
        assert task.target_id == "BVHIGH"
        assert task.status == TaskStatus.RUNNING
        assert task.lease_owner == "worker-1"
        assert task.lease_until == now + timedelta(seconds=120)

    await engine.dispose()


@pytest.mark.asyncio
async def test_task_repository_reuses_active_task_with_same_idempotency_key() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = CollectionTaskRepository(session)
        first = await repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BVDEDUP",
            priority=10,
            payload={"bvid": "BVDEDUP", "attempt": 1},
            not_before=now,
            idempotency_key="fetch_video_stats:video:BVDEDUP",
        )
        second = await repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BVDEDUP",
            priority=99,
            payload={"bvid": "BVDEDUP", "attempt": 2},
            not_before=now + timedelta(minutes=1),
            idempotency_key="fetch_video_stats:video:BVDEDUP",
        )
        await session.commit()

        tasks = list(await session.scalars(select(CollectionTask)))

        assert second.id == first.id
        assert len(tasks) == 1
        assert tasks[0].priority == 10
        assert tasks[0].payload["attempt"] == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_task_repository_persists_optional_cohort_links() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        task = await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BV-LINKED",
            priority=100,
            payload={"bvid": "BV-LINKED"},
            not_before=now,
            snapshot_cohort_id=11,
            snapshot_cohort_component_id=22,
        )

        assert task.snapshot_cohort_id == 11
        assert task.snapshot_cohort_component_id == 22

    await engine.dispose()


@pytest.mark.asyncio
async def test_task_repository_allows_reenqueue_after_success() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = CollectionTaskRepository(session)
        first = await repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BVDEDUP",
            priority=10,
            payload={"bvid": "BVDEDUP", "attempt": 1},
            not_before=now,
            idempotency_key="fetch_video_stats:video:BVDEDUP",
        )
        first.status = TaskStatus.SUCCEEDED
        await session.commit()

        second = await repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BVDEDUP",
            priority=99,
            payload={"bvid": "BVDEDUP", "attempt": 2},
            not_before=now + timedelta(minutes=1),
            idempotency_key="fetch_video_stats:video:BVDEDUP",
        )
        await session.commit()

        tasks = list(await session.scalars(select(CollectionTask)))

        assert second.id != first.id
        assert len(tasks) == 2
        assert second.priority == 99

    await engine.dispose()


@pytest.mark.asyncio
async def test_task_repository_lists_by_status_and_limit() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = CollectionTaskRepository(session)
        await repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BVPENDING",
            priority=100,
            payload={"bvid": "BVPENDING"},
            not_before=now,
        )
        failed = await repo.enqueue(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id="BVFAILED",
            priority=90,
            payload={"bvid": "BVFAILED"},
            not_before=now,
        )
        failed.status = TaskStatus.FAILED
        await session.commit()

        tasks = await repo.list_tasks(status=TaskStatus.FAILED, limit=10)

        assert [task.target_id for task in tasks] == ["BVFAILED"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_task_repository_retries_failed_tasks() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = CollectionTaskRepository(session)
        task = await repo.enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type="video",
            target_id="BVFAILED",
            priority=70,
            payload={"bvid": "BVFAILED"},
            not_before=now,
        )
        task.status = TaskStatus.FAILED
        task.retry_count = 3
        task.lease_owner = "dead-worker"
        task.lease_until = now + timedelta(minutes=5)
        await session.commit()

        retried = await repo.retry_failed(
            target_id="BVFAILED",
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            now=now + timedelta(minutes=1),
            limit=100,
        )
        await session.refresh(task)

        assert retried == 1
        assert task.status == TaskStatus.PENDING
        assert task.retry_count == 0
        assert task.not_before == now + timedelta(minutes=1)
        assert task.lease_owner is None
        assert task.lease_until is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_task_repository_recovers_expired_running_lease_without_retry() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = CollectionTaskRepository(session)
        task = await repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BVEXPIRED",
            priority=100,
            payload={"bvid": "BVEXPIRED"},
            not_before=now - timedelta(minutes=10),
        )
        task.status = TaskStatus.RUNNING
        task.retry_count = 2
        task.lease_owner = "dead-worker"
        task.lease_until = now - timedelta(seconds=1)
        await session.commit()

        recovered = await repo.recover_expired_leases(now=now, limit=100)
        await session.refresh(task)

        assert recovered == 1
        assert task.status == TaskStatus.PENDING
        assert task.retry_count == 2
        assert task.not_before == now
        assert task.lease_owner is None
        assert task.lease_until is None

    await engine.dispose()
