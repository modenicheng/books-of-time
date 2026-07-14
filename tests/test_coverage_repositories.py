from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.coverage import CoverageDraft
from books_of_time.db.base import Base
from books_of_time.db.models import CollectionRun
from books_of_time.db.repositories import (
    CollectionCoverageRepository,
    CollectionRunRepository,
    CollectionTaskRepository,
)
from books_of_time.domain.enums import TaskKind


async def _create_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_collection_run_repository_creates_and_updates_counts() -> None:
    engine, session_factory = await _create_session_factory()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            repo = CollectionRunRepository(session)

            run = await repo.get_or_create_running(
                run_id="run-1",
                worker_id="worker-1",
                now=now,
            )
            await repo.record_task_started(run, now=now + timedelta(seconds=1))
            await repo.record_task_succeeded(run, now=now + timedelta(seconds=2))
            await session.commit()

        async with session_factory() as session:
            saved = await session.scalar(select(CollectionRun))
            assert saved is not None
            assert saved.run_id == "run-1"
            assert saved.worker_id == "worker-1"
            assert saved.status == "succeeded"
            assert saved.tasks_started == 1
            assert saved.tasks_succeeded == 1
            assert saved.tasks_failed == 0
            assert saved.finished_at == now + timedelta(seconds=2)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collection_coverage_repository_inserts_and_lists_by_target() -> None:
    engine, session_factory = await _create_session_factory()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            task = await CollectionTaskRepository(session).enqueue(
                kind=TaskKind.FETCH_HOT_COMMENTS,
                target_type="video",
                target_id="BV1xx",
                priority=80,
                payload={"bvid": "BV1xx"},
                not_before=now,
                snapshot_cohort_id=11,
                snapshot_cohort_component_id=22,
            )
            draft = CoverageDraft(
                task_kind=TaskKind.FETCH_HOT_COMMENTS,
                target_type="video",
                target_id="BV1xx",
                pages_requested=1,
                pages_succeeded=1,
                items_observed=2,
                raw_payloads_saved=2,
                reason="complete",
            )
            await CollectionCoverageRepository(session).insert_from_draft(
                task=task,
                run_id="run-1",
                draft=draft,
                started_at=now,
                finished_at=now + timedelta(seconds=3),
            )
            await session.commit()

        async with session_factory() as session:
            rows = await CollectionCoverageRepository(session).list_for_target(
                target_type="video",
                target_id="BV1xx",
            )
            assert len(rows) == 1
            assert rows[0].status == "succeeded"
            assert rows[0].snapshot_cohort_id == 11
            assert rows[0].snapshot_cohort_component_id == 22
            assert rows[0].pages_requested == 1
            assert rows[0].pages_succeeded == 1
            assert rows[0].items_observed == 2
            assert rows[0].reason == "complete"
    finally:
        await engine.dispose()
