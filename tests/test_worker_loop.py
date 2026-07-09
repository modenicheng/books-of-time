from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.coverage import CoverageDraft
from books_of_time.db.base import Base
from books_of_time.db.models import CollectionTask
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind, TaskStatus
from books_of_time.worker import Worker


async def _create_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class SuccessfulCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        return CoverageDraft(
            task_kind=task.kind,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=1,
            pages_succeeded=1,
            items_observed=1,
            raw_payloads_saved=1,
            reason="complete",
        )


@pytest.mark.asyncio
async def test_worker_loop_runs_due_tasks_until_idle() -> None:
    engine, session_factory = await _create_session_factory()
    now = datetime(2000, 1, 1, tzinfo=UTC)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    try:
        async with session_factory() as session:
            repo = CollectionTaskRepository(session)
            await repo.enqueue(
                kind=TaskKind.FETCH_VIDEO_STATS,
                target_type="video",
                target_id="BV1",
                priority=100,
                payload={"bvid": "BV1"},
                not_before=now - timedelta(seconds=1),
            )
            await repo.enqueue(
                kind=TaskKind.FETCH_VIDEO_STATS,
                target_type="video",
                target_id="BV2",
                priority=90,
                payload={"bvid": "BV2"},
                not_before=now - timedelta(seconds=1),
            )
            await session.commit()

        worker = Worker(
            session_factory=session_factory,
            collectors={TaskKind.FETCH_VIDEO_STATS: SuccessfulCollector()},
            run_id="run-loop-test",
            lease_owner="worker-1",
        )

        executed = await worker.run_loop(
            idle_sleep_seconds=0.5,
            max_iterations=3,
            sleep=fake_sleep,
        )

        assert executed == 2
        assert slept == [0.5]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_recovers_expired_lease_before_leasing() -> None:
    engine, session_factory = await _create_session_factory()
    lease_time = datetime(2020, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            task = await CollectionTaskRepository(session).enqueue(
                kind=TaskKind.FETCH_VIDEO_STATS,
                target_type="video",
                target_id="BVSTUCK",
                priority=100,
                payload={"bvid": "BVSTUCK"},
                not_before=lease_time - timedelta(minutes=10),
            )
            task.status = TaskStatus.RUNNING
            task.lease_owner = "dead-worker"
            task.lease_until = lease_time
            await session.commit()

        worker = Worker(
            session_factory=session_factory,
            collectors={TaskKind.FETCH_VIDEO_STATS: SuccessfulCollector()},
            run_id="run-recover-test",
            lease_owner="worker-1",
        )

        executed = await worker.run_loop(
            max_iterations=1,
            sleep=lambda seconds: None,
        )

        async with session_factory() as session:
            task = await session.scalar(select(CollectionTask))

        assert executed == 1
        assert task is not None
        assert task.status == TaskStatus.SUCCEEDED
        assert task.lease_owner is None
        assert task.lease_until is None
    finally:
        await engine.dispose()
