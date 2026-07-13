from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.coverage import CoverageDraft
from books_of_time.db.base import Base
from books_of_time.db.models import (
    CollectionCoverageStat,
    CollectionRun,
    CollectionTask,
    HttpRequestAttempt,
    RequestBackoffState,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.http.errors import RequestErrorKind, RequestFailure
from books_of_time.http.evidence import current_http_evidence_sink
from books_of_time.storage.filesystem import RawPayloadFileStore
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


class FailingCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        raise RuntimeError("boom")


class RateLimitedCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        raise RequestFailure(
            kind=RequestErrorKind.RATE_LIMITED,
            request_type=BilibiliRequestType.VIDEO_STATS,
            message="rate limited",
            status_code=429,
            retry_after_seconds=45,
        )


class StartedAttemptThenFailingCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        sink = current_http_evidence_sink()
        assert sink is not None
        started_at = datetime(2099, 1, 1, tzinfo=UTC)
        await sink.begin(
            method="GET",
            url="https://api.bilibili.com/x/test",
            request_type=BilibiliRequestType.VIDEO_STATS,
            params={"bvid": task.target_id},
            request_started_at=started_at,
        )
        raise RuntimeError("collector stopped before raw persistence")


@pytest.mark.asyncio
async def test_worker_writes_run_and_coverage_on_success() -> None:
    engine, session_factory = await _create_session_factory()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            await CollectionTaskRepository(session).enqueue(
                kind=TaskKind.FETCH_VIDEO_STATS,
                target_type="video",
                target_id="BV1xx",
                priority=100,
                payload={"bvid": "BV1xx"},
                not_before=now - timedelta(seconds=1),
            )
            await session.commit()

        worker = Worker(
            session_factory=session_factory,
            collectors={TaskKind.FETCH_VIDEO_STATS: SuccessfulCollector()},
            run_id="run-1",
            lease_owner="worker-1",
        )
        assert await worker.run_once(now=now) is True

        async with session_factory() as session:
            run = await session.scalar(select(CollectionRun))
            stat = await session.scalar(select(CollectionCoverageStat))
            task = await session.scalar(select(CollectionTask))
            assert run is not None
            assert run.tasks_started == 1
            assert run.tasks_succeeded == 1
            assert run.tasks_failed == 0
            assert stat is not None
            assert stat.run_id == "run-1"
            assert stat.status == "succeeded"
            assert stat.reason == "complete"
            assert task is not None
            assert task.status == TaskStatus.SUCCEEDED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_uses_request_failure_backoff_for_retry() -> None:
    engine, session_factory = await _create_session_factory()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            await CollectionTaskRepository(session).enqueue(
                kind=TaskKind.FETCH_VIDEO_STATS,
                target_type="video",
                target_id="BV1xx",
                priority=100,
                payload={"bvid": "BV1xx"},
                not_before=now - timedelta(seconds=1),
                max_retries=3,
            )
            await session.commit()

        worker = Worker(
            session_factory=session_factory,
            collectors={TaskKind.FETCH_VIDEO_STATS: RateLimitedCollector()},
            run_id="run-1",
            lease_owner="worker-1",
            retry_delay_seconds=30,
        )

        result = await worker.run_once(now=now)
        assert result is True

        async with session_factory() as session:
            stat = await session.scalar(select(CollectionCoverageStat))
            task = await session.scalar(select(CollectionTask))
            backoff = await session.scalar(select(RequestBackoffState))

            assert stat is not None
            assert stat.status == "failed"
            assert stat.reason == "429"
            assert stat.extra["request_type"] == BilibiliRequestType.VIDEO_STATS
            assert stat.extra["status_code"] == 429
            assert task is not None
            assert task.status == TaskStatus.PENDING
            assert task.retry_count == 1
            assert task.not_before == now + timedelta(seconds=45)
            assert backoff is not None
            assert backoff.error_kind == "429"
            assert backoff.backoff_until == now + timedelta(seconds=45)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_writes_failed_coverage_and_preserves_retry() -> None:
    engine, session_factory = await _create_session_factory()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            await CollectionTaskRepository(session).enqueue(
                kind=TaskKind.FETCH_VIDEO_STATS,
                target_type="video",
                target_id="BV1xx",
                priority=100,
                payload={"bvid": "BV1xx"},
                not_before=now - timedelta(seconds=1),
                max_retries=3,
            )
            await session.commit()

        worker = Worker(
            session_factory=session_factory,
            collectors={TaskKind.FETCH_VIDEO_STATS: FailingCollector()},
            run_id="run-1",
            lease_owner="worker-1",
            retry_delay_seconds=30,
        )

        result = await worker.run_once(now=now)
        assert result is True

        async with session_factory() as session:
            run = await session.scalar(select(CollectionRun))
            stat = await session.scalar(select(CollectionCoverageStat))
            task = await session.scalar(select(CollectionTask))
            assert run is not None
            assert run.tasks_started == 1
            assert run.tasks_succeeded == 0
            assert run.tasks_failed == 1
            assert stat is not None
            assert stat.status == "failed"
            assert stat.reason == "collector_exception"
            assert stat.extra["exception_type"] == "RuntimeError"
            assert task is not None
            assert task.status == TaskStatus.PENDING
            assert task.retry_count == 1
            assert task.not_before == now + timedelta(seconds=30)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_marks_unfinished_http_attempt_abandoned(
    tmp_path,
) -> None:
    engine, session_factory = await _create_session_factory()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            task = await CollectionTaskRepository(session).enqueue(
                kind=TaskKind.FETCH_VIDEO_STATS,
                target_type="video",
                target_id="BV1xx",
                priority=100,
                payload={"bvid": "BV1xx"},
                not_before=now - timedelta(seconds=1),
            )
            await session.commit()

        worker = Worker(
            session_factory=session_factory,
            collectors={
                TaskKind.FETCH_VIDEO_STATS: StartedAttemptThenFailingCollector()
            },
            run_id="run-1",
            lease_owner="worker-1",
            raw_store=RawPayloadFileStore(tmp_path / "raw"),
        )
        assert await worker.run_once(now=now) is True

        async with session_factory() as session:
            attempt = await session.scalar(select(HttpRequestAttempt))

        assert attempt is not None
        assert attempt.collection_task_id == task.id
        assert attempt.status == "abandoned"
        assert attempt.error_type == "collector_abort"
        assert attempt.raw_payload_id is None
    finally:
        await engine.dispose()
