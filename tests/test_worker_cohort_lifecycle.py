from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.coverage import CoverageDraft
from books_of_time.db.base import Base
from books_of_time.db.cohort_repositories import (
    CohortComponentPlan,
    SnapshotCohortPlan,
    SnapshotCohortRepository,
)
from books_of_time.db.models import (
    CollectionCoverageStat,
    CollectionPolicyVersion,
    CollectionTask,
    HttpRequestAttempt,
    KnownVideo,
    SnapshotCohort,
    SnapshotCohortComponent,
    VideoCollectionState,
)
from books_of_time.domain.cohort_policy import (
    CohortComponentStatus,
    CohortRolloutMode,
    CohortStatus,
    CollectionTier,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.http.evidence import current_http_evidence_sink
from books_of_time.storage.filesystem import RawPayloadFileStore
from books_of_time.worker import Worker


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _materialize_task(
    session,
    *,
    now: datetime,
    bvid: str = "BV-WORKER-C3",
    max_retries: int = 3,
) -> tuple[int, int, int]:
    session.add(
        CollectionPolicyVersion(
            version="cohort-default-v1",
            policy_kind="snapshot_cohort",
            scope_type="global",
            scope_id="global",
            timezone="Asia/Shanghai",
            policy={},
            algorithm="configured-fixed-v1",
            created_at=now,
            activated_at=now,
            active=True,
        )
    )
    session.add(
        KnownVideo(
            bvid=bvid,
            source_mid="42",
            pubdate=now - timedelta(hours=1),
            first_seen_at=now - timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    session.add(
        VideoCollectionState(
            bvid=bvid,
            desired_tier="s",
            effective_tier="s",
            consecutive_downgrade_count=0,
            life_stage="active",
            schedule_anchor_at=now - timedelta(hours=1),
            policy_version="cohort-default-v1",
            extra={},
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    result = await SnapshotCohortRepository(session).materialize(
        SnapshotCohortPlan(
            cohort_key=f"snapshot:{bvid}:2026-07-14T04:00:00Z:routine",
            bvid=bvid,
            scheduled_for=now - timedelta(seconds=10),
            reason="routine",
            age_checkpoint_hours=None,
            desired_tier=CollectionTier.S,
            effective_tier=CollectionTier.S,
            policy_version="cohort-default-v1",
            deadline=now + timedelta(minutes=2),
            status=CohortStatus.PLANNED,
            status_reason=None,
            extra={},
            components=(
                CohortComponentPlan(
                    "video_metrics",
                    TaskKind.FETCH_VIDEO_STATS,
                    1,
                    priority=100,
                    max_retries=max_retries,
                ),
            ),
        ),
        rollout_mode=CohortRolloutMode.LIVE,
        now=now - timedelta(seconds=10),
    )
    await session.commit()
    return result.tasks[0].id, result.cohort.id, result.components[0].id


class SuccessfulCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        return CoverageDraft(
            task_kind=task.kind,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=1,
            pages_succeeded=1,
            items_observed=3,
            raw_payloads_saved=1,
            reason="complete",
        )


class PartialCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        return CoverageDraft(
            task_kind=task.kind,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=2,
            pages_succeeded=1,
            items_observed=3,
            raw_payloads_saved=1,
            truncated=True,
            reason="time_budget",
        )


class CorruptedCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        return CoverageDraft(
            task_kind=task.kind,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=1,
            pages_succeeded=1,
            raw_payloads_saved=1,
            corrupted=True,
            reason="cursor_loop",
        )


class FailingCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        raise RuntimeError("boom")


class AttemptThenFailingCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        sink = current_http_evidence_sink()
        assert sink is not None
        for offset in (7, 12):
            await sink.begin(
                method="GET",
                url="https://api.bilibili.com/x/test",
                request_type=BilibiliRequestType.VIDEO_STATS,
                params={"bvid": task.target_id, "offset": offset},
                request_started_at=datetime(2099, 1, 1, tzinfo=UTC)
                + timedelta(seconds=offset),
            )
        raise RuntimeError("after request start")


@pytest.mark.asyncio
async def test_worker_success_completes_component_cohort_and_evidence_links() -> None:
    engine, session_factory = await _database()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            task_id, cohort_id, component_id = await _materialize_task(
                session,
                now=now,
            )

        worker = Worker(
            session_factory=session_factory,
            collectors={TaskKind.FETCH_VIDEO_STATS: SuccessfulCollector()},
            run_id="cohort-success",
            lease_owner="worker-c3",
        )
        assert await worker.run_once(now=now) is True

        async with session_factory() as session:
            task = await session.get(CollectionTask, task_id)
            cohort = await session.get(SnapshotCohort, cohort_id)
            component = await session.get(SnapshotCohortComponent, component_id)
            coverage = await session.scalar(select(CollectionCoverageStat))
            state = await session.get(VideoCollectionState, "BV-WORKER-C3")

            assert task is not None and task.status is TaskStatus.SUCCEEDED
            assert component is not None
            assert component.status == CohortComponentStatus.COMPLETE.value
            assert component.started_at == now
            assert component.skew_seconds is None
            assert component.finished_at is not None
            assert component.requested_pages == 1
            assert component.succeeded_pages == 1
            assert component.items_observed == 3
            assert component.raw_payloads_saved == 1
            assert cohort is not None and cohort.status == CohortStatus.COMPLETE.value
            assert cohort.started_at == now
            assert cohort.finished_at == component.finished_at
            assert cohort.completed_component_count == 1
            assert coverage is not None
            assert coverage.snapshot_cohort_id == cohort_id
            assert coverage.snapshot_cohort_component_id == component_id
            assert state is not None
            assert state.last_completed_cohort_at == cohort.finished_at
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collector", "component_status", "cohort_status"),
    [
        (PartialCollector(), "partial", "partial"),
        (CorruptedCollector(), "corrupted", "corrupted"),
    ],
)
async def test_worker_maps_partial_and_corrupted_coverage_to_cohort(
    collector,
    component_status: str,
    cohort_status: str,
) -> None:
    engine, session_factory = await _database()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            _task_id, cohort_id, component_id = await _materialize_task(
                session,
                now=now,
            )
        worker = Worker(
            session_factory=session_factory,
            collectors={TaskKind.FETCH_VIDEO_STATS: collector},
            run_id=f"cohort-{component_status}",
            lease_owner="worker-c3",
        )

        assert await worker.run_once(now=now) is True

        async with session_factory() as session:
            cohort = await session.get(SnapshotCohort, cohort_id)
            component = await session.get(SnapshotCohortComponent, component_id)
            assert component is not None and component.status == component_status
            assert cohort is not None and cohort.status == cohort_status
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_retry_keeps_component_running_then_terminal_failure_finishes_it() -> (
    None
):
    engine, session_factory = await _database()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            task_id, cohort_id, component_id = await _materialize_task(
                session,
                now=now,
                max_retries=1,
            )
        worker = Worker(
            session_factory=session_factory,
            collectors={TaskKind.FETCH_VIDEO_STATS: FailingCollector()},
            run_id="cohort-failure",
            lease_owner="worker-c3",
            retry_delay_seconds=1,
        )

        assert await worker.run_once(now=now) is True
        async with session_factory() as session:
            task = await session.get(CollectionTask, task_id)
            cohort = await session.get(SnapshotCohort, cohort_id)
            component = await session.get(SnapshotCohortComponent, component_id)
            assert task is not None and task.status is TaskStatus.PENDING
            assert component is not None and component.status == "running"
            assert component.finished_at is None
            assert cohort is not None and cohort.status == "running"

        assert await worker.run_once(now=now + timedelta(seconds=1)) is True
        async with session_factory() as session:
            task = await session.get(CollectionTask, task_id)
            cohort = await session.get(SnapshotCohort, cohort_id)
            component = await session.get(SnapshotCohortComponent, component_id)
            assert task is not None and task.status is TaskStatus.FAILED
            assert component is not None and component.status == "failed"
            assert component.finished_at is not None
            assert cohort is not None and cohort.status == "partial"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_http_attempt_inherits_cohort_links(tmp_path) -> None:
    engine, session_factory = await _database()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            _task_id, cohort_id, component_id = await _materialize_task(
                session,
                now=now,
            )
        worker = Worker(
            session_factory=session_factory,
            collectors={TaskKind.FETCH_VIDEO_STATS: AttemptThenFailingCollector()},
            run_id="cohort-attempt",
            lease_owner="worker-c3",
            raw_store=RawPayloadFileStore(tmp_path / "raw"),
        )

        assert await worker.run_once(now=now) is True

        async with session_factory() as session:
            attempts = list(
                await session.scalars(
                    select(HttpRequestAttempt).order_by(HttpRequestAttempt.id)
                )
            )
            component = await session.get(SnapshotCohortComponent, component_id)
            assert len(attempts) == 2
            assert all(attempt.snapshot_cohort_id == cohort_id for attempt in attempts)
            assert all(
                attempt.snapshot_cohort_component_id == component_id
                for attempt in attempts
            )
            assert all(attempt.status == "abandoned" for attempt in attempts)
            assert component is not None
            assert component.skew_seconds == 17
    finally:
        await engine.dispose()
