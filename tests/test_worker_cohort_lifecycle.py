from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.coverage import CoverageDraft
from books_of_time.db.base import Base
from books_of_time.db.cohort_repositories import (
    CohortComponentPlan,
    SnapshotCohortExecutionRepository,
    SnapshotCohortPlan,
    SnapshotCohortRepository,
)
from books_of_time.db.comment_scan_repositories import CommentScanRunRepository
from books_of_time.db.latest_scan_repositories import LatestScanRunRepository
from books_of_time.db.models import (
    CollectionCoverageStat,
    CollectionPolicyVersion,
    CollectionTask,
    CommentScanRun,
    FrontierState,
    HttpRequestAttempt,
    KnownVideo,
    RawPayload,
    SnapshotCohort,
    SnapshotCohortComponent,
    VideoCollectionState,
)
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    FrontierStateRepository,
    FrontierStateUpdate,
)
from books_of_time.domain.cohort_policy import (
    CohortComponentStatus,
    CohortRolloutMode,
    CohortStatus,
    CollectionTier,
)
from books_of_time.domain.enums import (
    BilibiliRequestType,
    CommentScanStatus,
    TaskKind,
    TaskStatus,
)
from books_of_time.http.errors import ParseFailure
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


async def _materialize_hot_scan_task(
    session,
    *,
    now: datetime,
    bvid: str,
    max_retries: int = 3,
) -> tuple[int, int, int, int]:
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
    scan_settings = {
        "scan_mode": "hot_deep",
        "start_page": 4,
        "end_page": 20,
        "target_pages": 17,
        "max_pages_per_slice": 10,
        "max_scan_seconds": 55,
    }
    result = await SnapshotCohortRepository(session).materialize(
        SnapshotCohortPlan(
            cohort_key=f"snapshot:{bvid}:age:6h",
            bvid=bvid,
            scheduled_for=now - timedelta(seconds=10),
            reason="age_checkpoint",
            age_checkpoint_hours=6,
            desired_tier=CollectionTier.S,
            effective_tier=CollectionTier.S,
            policy_version="cohort-default-v1",
            deadline=now + timedelta(minutes=2),
            status=CohortStatus.PLANNED,
            status_reason=None,
            extra={},
            components=(
                CohortComponentPlan(
                    "hot_deep",
                    TaskKind.FETCH_HOT_COMMENTS,
                    17,
                    priority=120,
                    max_retries=max_retries,
                    payload={
                        **scan_settings,
                        "page": 4,
                        "page_limit": 17,
                        "aid": 777,
                    },
                    extra=scan_settings,
                ),
            ),
        ),
        rollout_mode=CohortRolloutMode.LIVE,
        now=now - timedelta(seconds=10),
    )
    scan_id = result.components[0].comment_scan_run_id
    assert scan_id is not None
    await session.commit()
    return result.tasks[0].id, result.cohort.id, result.components[0].id, scan_id


async def _seed_latest_graph(session, *, now: datetime, bvid: str) -> None:
    session.add(
        CollectionPolicyVersion(
            version="cohort-latest-v1",
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
            policy_version="cohort-latest-v1",
            extra={},
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()


def _latest_cohort_plan(
    *,
    bvid: str,
    scheduled_for: datetime,
    deadline: datetime,
    suffix: str,
    max_retries: int = 3,
) -> SnapshotCohortPlan:
    return SnapshotCohortPlan(
        cohort_key=f"snapshot:{bvid}:{suffix}",
        bvid=bvid,
        scheduled_for=scheduled_for,
        reason="routine",
        age_checkpoint_hours=None,
        desired_tier=CollectionTier.S,
        effective_tier=CollectionTier.S,
        policy_version="cohort-latest-v1",
        deadline=deadline,
        status=CohortStatus.PLANNED,
        status_reason=None,
        extra={},
        components=(
            CohortComponentPlan(
                "latest_current_head",
                TaskKind.FETCH_LATEST_COMMENTS,
                1,
                priority=100,
                max_retries=max_retries,
                payload={
                    "max_scan_seconds": 48,
                    "current_head_required": True,
                },
                extra={
                    "max_scan_seconds": 48,
                    "current_head_required": True,
                },
            ),
        ),
    )


async def _materialize_shared_latest_task(
    session,
    *,
    now: datetime,
    bvid: str,
    max_retries: int,
) -> tuple[int, int, tuple[int, int], tuple[int, int]]:
    await _seed_latest_graph(session, now=now, bvid=bvid)
    repository = SnapshotCohortRepository(session)
    first = await repository.materialize(
        _latest_cohort_plan(
            bvid=bvid,
            scheduled_for=now,
            deadline=now + timedelta(minutes=2),
            suffix="first",
            max_retries=max_retries,
        ),
        rollout_mode=CohortRolloutMode.LIVE,
        now=now,
    )
    second = await repository.materialize(
        _latest_cohort_plan(
            bvid=bvid,
            scheduled_for=now + timedelta(seconds=10),
            deadline=now + timedelta(minutes=2, seconds=10),
            suffix="second",
            max_retries=max_retries,
        ),
        rollout_mode=CohortRolloutMode.LIVE,
        now=now + timedelta(seconds=10),
    )
    scan_id = first.components[0].comment_scan_run_id
    assert scan_id is not None
    assert second.components[0].comment_scan_run_id == scan_id
    return (
        first.tasks[0].id,
        scan_id,
        (first.cohort.id, first.components[0].id),
        (second.cohort.id, second.components[0].id),
    )


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


class TwoSliceScanCollector:
    def __init__(self, now: datetime) -> None:
        self.now = now

    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        assert task.comment_scan_run_id is not None
        assert task.scan_slice_no is not None
        repository = CommentScanRunRepository(session)
        scan = await repository.mark_running(
            task.comment_scan_run_id,
            now=self.now,
            oid=777,
        )
        if task.scan_slice_no == 0:
            page_numbers = range(4, 14)
        else:
            page_numbers = range(14, 21)
        for page in page_numbers:
            await repository.record_page_requested(
                scan.id,
                page_number=page,
                now=self.now,
            )
            scan = await repository.record_page_succeeded(
                scan.id,
                page_number=page,
                items_observed=1,
                raw_payloads_saved=1,
                now=self.now,
            )
        page_count = len(page_numbers)
        if task.scan_slice_no == 0:
            scan = await repository.mark_paused(
                scan.id,
                outcome="time_slice_yield",
                now=self.now,
            )
            await CollectionTaskRepository(session).enqueue(
                kind=task.kind,
                target_type=task.target_type,
                target_id=task.target_id,
                priority=task.priority,
                budget_cost=task.budget_cost,
                payload={**task.payload, "page": 14},
                not_before=self.now,
                max_retries=task.max_retries,
                idempotency_key=f"{scan.scan_key}:hot_deep:active:1",
                snapshot_cohort_id=task.snapshot_cohort_id,
                snapshot_cohort_component_id=task.snapshot_cohort_component_id,
                comment_scan_run_id=scan.id,
                scan_slice_no=1,
                scan_slice_key=f"{scan.id}:hot_deep:1",
            )
            return CoverageDraft(
                task_kind=task.kind,
                target_type=task.target_type,
                target_id=task.target_id,
                pages_requested=page_count,
                pages_succeeded=page_count,
                items_observed=page_count,
                raw_payloads_saved=page_count,
                truncated=True,
                reason="time_slice_yield",
            )
        await repository.mark_complete(
            scan.id,
            outcome="target_reached",
            now=self.now,
        )
        return CoverageDraft(
            task_kind=task.kind,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=page_count,
            pages_succeeded=page_count,
            items_observed=page_count,
            raw_payloads_saved=page_count,
            reason="target_reached",
        )


class TerminalFailingScanCollector:
    def __init__(self, now: datetime) -> None:
        self.now = now

    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        assert task.comment_scan_run_id is not None
        repository = CommentScanRunRepository(session)
        scan = await repository.mark_running(
            task.comment_scan_run_id,
            now=self.now,
            oid=777,
        )
        await repository.record_page_requested(
            scan.id,
            page_number=4,
            now=self.now,
        )
        await repository.record_page_failed(
            scan.id,
            page_number=4,
            error_type="RuntimeError",
            error_message="boom",
            now=self.now,
        )
        raise RuntimeError("boom")


class ParseFailingScanCollector:
    def __init__(self, now: datetime) -> None:
        self.now = now

    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        assert task.comment_scan_run_id is not None
        repository = CommentScanRunRepository(session)
        scan = await repository.mark_running(
            task.comment_scan_run_id,
            now=self.now,
            oid=777,
        )
        await repository.record_page_requested(
            scan.id,
            page_number=4,
            now=self.now,
        )
        body_hash = hashlib.sha256(b"invalid-json").digest()
        session.add(
            RawPayload(
                captured_at=self.now,
                request_type=BilibiliRequestType.COMMENT_HOT,
                method="GET",
                url_hash=hashlib.sha256(b"https://example.test/hot").digest(),
                params_hash=None,
                status_code=200,
                payload_hash=body_hash,
                storage_uri="file://raw/invalid.json",
                compressed_size=12,
                uncompressed_size=12,
                parser_version="test",
                created_at=self.now,
            )
        )
        await session.flush()
        await repository.record_page_failed(
            scan.id,
            page_number=4,
            error_type="ParseFailure",
            error_message="invalid comment page",
            now=self.now,
        )
        raise ParseFailure(
            request_type=BilibiliRequestType.COMMENT_HOT,
            message="invalid comment page",
            status_code=200,
        )


class FailingLatestScanCollector:
    def __init__(self, now: datetime, *, parse_failure: bool = False) -> None:
        self.now = now
        self.parse_failure = parse_failure

    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        assert task.comment_scan_run_id is not None
        repository = LatestScanRunRepository(session)
        scan = await repository.mark_running(
            task.comment_scan_run_id,
            now=self.now,
            oid=777,
        )
        await repository.record_page_requested(scan.id, now=self.now)
        if self.parse_failure:
            raise ParseFailure(
                request_type=BilibiliRequestType.COMMENT_LATEST,
                message="invalid latest comment page",
                status_code=200,
            )
        raise RuntimeError("latest collector failed")


class StaleFailingLatestScanCollector:
    def __init__(self, now: datetime) -> None:
        self.now = now

    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        assert task.comment_scan_run_id is not None
        scan_repository = LatestScanRunRepository(session)
        scan = await scan_repository.mark_running(
            task.comment_scan_run_id,
            now=self.now,
            oid=777,
        )
        await scan_repository.record_page_requested(scan.id, now=self.now)
        frontier_repository = FrontierStateRepository(session)
        frontier = await frontier_repository.get_or_create(
            target_type="video",
            target_id=scan.bvid,
            frontier_type="latest_comments",
            now=self.now,
            lock=True,
        )
        await frontier_repository.compare_and_swap(
            frontier.id,
            frontier.version,
            FrontierStateUpdate(
                frontier_rpid=frontier.frontier_rpid,
                frontier_time=frontier.frontier_time,
                frontier_anchor_set=frontier.frontier_anchor_set,
                active_scan_run_id=scan.id,
                cursor=frontier.cursor,
                last_scan_at=self.now,
                last_scan_status=CommentScanStatus.RUNNING.value,
                last_scan_pages=scan.pages_succeeded,
                last_scan_truncated=True,
                extra=frontier.extra,
            ),
            now=self.now,
        )
        raise RuntimeError("stale latest worker failed")


@pytest.mark.asyncio
async def test_latest_head_capture_fans_out_to_each_consumer_window() -> None:
    engine, session_factory = await _database()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    bvid = "BV-LATEST-FANOUT"
    try:
        async with session_factory.begin() as session:
            await _seed_latest_graph(session, now=now, bvid=bvid)
            repository = SnapshotCohortRepository(session)
            first = await repository.materialize(
                _latest_cohort_plan(
                    bvid=bvid,
                    scheduled_for=now,
                    deadline=now + timedelta(minutes=2),
                    suffix="first",
                ),
                rollout_mode=CohortRolloutMode.LIVE,
                now=now,
            )
            second = await repository.materialize(
                _latest_cohort_plan(
                    bvid=bvid,
                    scheduled_for=now + timedelta(seconds=10),
                    deadline=now + timedelta(minutes=2, seconds=10),
                    suffix="second",
                ),
                rollout_mode=CohortRolloutMode.LIVE,
                now=now + timedelta(seconds=10),
            )
            parent_id = first.components[0].comment_scan_run_id
            assert parent_id is not None
            assert second.components[0].comment_scan_run_id == parent_id

            latest_repository = LatestScanRunRepository(session)
            parent = await latest_repository.mark_running(parent_id, now=now)
            parent.start_anchor_set = [
                {"rpid": 9001, "platform_created_at": now.isoformat()}
            ]
            parent.start_frontier_rpid = 9001
            parent.pages_requested = 2
            parent.pages_succeeded = 2
            parent.items_observed = 5
            parent.raw_payloads_saved = 2
            frontier = await session.scalar(select(FrontierState))
            assert frontier is not None
            handoff = await latest_repository.complete_tail_and_create_head(
                parent.id,
                frontier_state=frontier,
                expected_version=frontier.version,
                now=now + timedelta(seconds=30),
            )
            assert handoff is not None
            child = handoff.scan

            execution = SnapshotCohortExecutionRepository(session)
            await execution.sync_latest_scan_consumers(
                parent.id,
                finished_at=now + timedelta(seconds=30),
            )
            early_components = list(
                await session.scalars(
                    select(SnapshotCohortComponent).order_by(SnapshotCohortComponent.id)
                )
            )
            early_cohorts = list(
                await session.scalars(
                    select(SnapshotCohort).order_by(SnapshotCohort.id)
                )
            )
            assert all(
                component.comment_scan_run_id == child.id
                for component in early_components
            )
            assert all(
                component.status == CohortComponentStatus.JOINED_ACTIVE_TASK.value
                for component in early_components
            )
            assert all(
                cohort.status == CohortStatus.RUNNING.value for cohort in early_cohorts
            )

            captured_at = now + timedelta(seconds=40)
            child.status = CommentScanStatus.PAUSED
            child.started_at = now + timedelta(seconds=30)
            child.pages_requested = 1
            child.pages_succeeded = 1
            child.items_observed = 3
            child.raw_payloads_saved = 1
            child.extra = {
                **child.extra,
                "head_captured_at": captured_at.isoformat(),
            }
            third = await repository.materialize(
                _latest_cohort_plan(
                    bvid=bvid,
                    scheduled_for=now + timedelta(seconds=50),
                    deadline=now + timedelta(minutes=2, seconds=50),
                    suffix="third",
                ),
                rollout_mode=CohortRolloutMode.LIVE,
                now=now + timedelta(seconds=50),
            )
            assert third.components[0].comment_scan_run_id == child.id

            await execution.sync_latest_scan_consumers(
                child.id,
                finished_at=now + timedelta(seconds=55),
            )
            components = list(
                await session.scalars(
                    select(SnapshotCohortComponent).order_by(SnapshotCohortComponent.id)
                )
            )
            cohorts = list(
                await session.scalars(
                    select(SnapshotCohort).order_by(SnapshotCohort.id)
                )
            )

            assert [component.status for component in components] == [
                CohortComponentStatus.COMPLETE.value,
                CohortComponentStatus.COMPLETE.value,
                CohortComponentStatus.JOINED_ACTIVE_TASK.value,
            ]
            assert all(component.requested_pages == 3 for component in components)
            assert all(component.succeeded_pages == 3 for component in components)
            assert all(component.items_observed == 8 for component in components)
            assert all(component.raw_payloads_saved == 3 for component in components)
            assert components[2].finished_at is None
            assert [cohort.status for cohort in cohorts] == [
                CohortStatus.COMPLETE.value,
                CohortStatus.COMPLETE.value,
                CohortStatus.RUNNING.value,
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_latest_retry_failure_keeps_all_consumers_and_owner_active() -> None:
    engine, session_factory = await _database()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            task_id, scan_id, first, second = await _materialize_shared_latest_task(
                session,
                now=now,
                bvid="BV-LATEST-RETRY",
                max_retries=1,
            )

        worker = Worker(
            session_factory=session_factory,
            collectors={
                TaskKind.FETCH_LATEST_COMMENTS: FailingLatestScanCollector(now),
            },
            run_id="latest-retry-active",
            lease_owner="worker-c5",
            retry_delay_seconds=1,
        )
        assert await worker.run_once(now=now) is True

        async with session_factory() as session:
            task = await session.get(CollectionTask, task_id)
            scan = await session.get(CommentScanRun, scan_id)
            frontier = await session.scalar(select(FrontierState))
            components = [
                await session.get(SnapshotCohortComponent, component_id)
                for _cohort_id, component_id in (first, second)
            ]
            cohorts = [
                await session.get(SnapshotCohort, cohort_id)
                for cohort_id, _component_id in (first, second)
            ]

            assert task is not None and task.status is TaskStatus.PENDING
            assert scan is not None and scan.status is CommentScanStatus.RUNNING
            assert frontier is not None and frontier.active_scan_run_id == scan_id
            assert all(component is not None for component in components)
            assert all(
                component.status
                in {
                    CohortComponentStatus.RUNNING.value,
                    CohortComponentStatus.JOINED_ACTIVE_TASK.value,
                }
                for component in components
                if component is not None
            )
            assert all(
                cohort is not None and cohort.status == CohortStatus.RUNNING.value
                for cohort in cohorts
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parse_failure", "scan_status", "component_status", "cohort_status"),
    [
        (
            False,
            CommentScanStatus.FAILED,
            CohortComponentStatus.FAILED.value,
            CohortStatus.PARTIAL.value,
        ),
        (
            True,
            CommentScanStatus.CORRUPTED,
            CohortComponentStatus.CORRUPTED.value,
            CohortStatus.CORRUPTED.value,
        ),
    ],
)
async def test_latest_terminal_failure_clears_owner_and_fans_out(
    parse_failure: bool,
    scan_status: CommentScanStatus,
    component_status: str,
    cohort_status: str,
) -> None:
    engine, session_factory = await _database()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            task_id, scan_id, first, second = await _materialize_shared_latest_task(
                session,
                now=now,
                bvid=f"BV-LATEST-TERMINAL-{int(parse_failure)}",
                max_retries=0,
            )

        worker = Worker(
            session_factory=session_factory,
            collectors={
                TaskKind.FETCH_LATEST_COMMENTS: FailingLatestScanCollector(
                    now,
                    parse_failure=parse_failure,
                ),
            },
            run_id=f"latest-terminal-{int(parse_failure)}",
            lease_owner="worker-c5",
        )
        assert await worker.run_once(now=now) is True

        async with session_factory() as session:
            task = await session.get(CollectionTask, task_id)
            scan = await session.get(CommentScanRun, scan_id)
            frontier = await session.scalar(select(FrontierState))
            components = [
                await session.get(SnapshotCohortComponent, component_id)
                for _cohort_id, component_id in (first, second)
            ]
            cohorts = [
                await session.get(SnapshotCohort, cohort_id)
                for cohort_id, _component_id in (first, second)
            ]
            coverage = await session.scalar(select(CollectionCoverageStat))

            assert task is not None and task.status is TaskStatus.FAILED
            assert scan is not None and scan.status is scan_status
            assert scan.outcome == "retry_exhausted"
            assert frontier is not None and frontier.active_scan_run_id is None
            assert all(
                component is not None and component.status == component_status
                for component in components
            )
            assert all(
                cohort is not None and cohort.status == cohort_status
                for cohort in cohorts
            )
            assert coverage is not None
            assert coverage.comment_scan_run_id == scan_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_latest_worker_cannot_clear_newer_frontier_version() -> None:
    engine, session_factory = await _database()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            task_id, scan_id, first, second = await _materialize_shared_latest_task(
                session,
                now=now,
                bvid="BV-LATEST-STALE-WORKER",
                max_retries=0,
            )

        worker = Worker(
            session_factory=session_factory,
            collectors={
                TaskKind.FETCH_LATEST_COMMENTS: StaleFailingLatestScanCollector(now),
            },
            run_id="latest-stale-worker",
            lease_owner="worker-c5-stale",
        )
        assert await worker.run_once(now=now) is True

        async with session_factory() as session:
            task = await session.get(CollectionTask, task_id)
            scan = await session.get(CommentScanRun, scan_id)
            frontier = await session.scalar(select(FrontierState))
            components = [
                await session.get(SnapshotCohortComponent, component_id)
                for _cohort_id, component_id in (first, second)
            ]

            assert task is not None and task.status is TaskStatus.FAILED
            assert scan is not None and scan.status is CommentScanStatus.RUNNING
            assert frontier is not None
            assert frontier.active_scan_run_id == scan_id
            assert frontier.version > int(task.payload["frontier_version"])
            assert all(
                component is not None
                and component.status == CohortComponentStatus.JOINED_ACTIVE_TASK.value
                for component in components
            )
    finally:
        await engine.dispose()


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
async def test_scan_slices_keep_component_running_until_logical_scan_completes() -> (
    None
):
    engine, session_factory = await _database()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            (
                first_task_id,
                cohort_id,
                component_id,
                scan_id,
            ) = await _materialize_hot_scan_task(
                session,
                now=now,
                bvid="BV-SCAN-LIFECYCLE",
            )
        worker = Worker(
            session_factory=session_factory,
            collectors={
                TaskKind.FETCH_HOT_COMMENTS: TwoSliceScanCollector(now),
            },
            run_id="cohort-scan-slices",
            lease_owner="worker-c4",
        )

        assert await worker.run_once(now=now) is True
        async with session_factory() as session:
            first_task = await session.get(CollectionTask, first_task_id)
            scan = await session.get(CommentScanRun, scan_id)
            component = await session.get(SnapshotCohortComponent, component_id)
            cohort = await session.get(SnapshotCohort, cohort_id)
            coverages = list(await session.scalars(select(CollectionCoverageStat)))
            follow_up = await session.scalar(
                select(CollectionTask).where(CollectionTask.scan_slice_no == 1)
            )

            assert first_task is not None and first_task.status is TaskStatus.SUCCEEDED
            assert follow_up is not None and follow_up.status is TaskStatus.PENDING
            assert scan is not None and scan.status is CommentScanStatus.PAUSED
            assert component is not None
            assert component.status == CohortComponentStatus.RUNNING.value
            assert component.finished_at is None
            assert component.requested_pages == 10
            assert component.succeeded_pages == 10
            assert component.items_observed == 10
            assert component.raw_payloads_saved == 10
            assert cohort is not None and cohort.status == CohortStatus.RUNNING.value
            assert cohort.finished_at is None
            assert len(coverages) == 1
            assert coverages[0].status == "partial"
            assert coverages[0].reason == "time_slice_yield"
            assert coverages[0].comment_scan_run_id == scan_id

        assert await worker.run_once(now=now + timedelta(seconds=1)) is True
        async with session_factory() as session:
            scan = await session.get(CommentScanRun, scan_id)
            component = await session.get(SnapshotCohortComponent, component_id)
            cohort = await session.get(SnapshotCohort, cohort_id)
            coverages = list(
                await session.scalars(
                    select(CollectionCoverageStat).order_by(CollectionCoverageStat.id)
                )
            )

            assert scan is not None and scan.status is CommentScanStatus.COMPLETE
            assert component is not None
            assert component.status == CohortComponentStatus.COMPLETE.value
            assert component.requested_pages == 17
            assert component.succeeded_pages == 17
            assert component.items_observed == 17
            assert component.raw_payloads_saved == 17
            assert component.finished_at is not None
            assert cohort is not None and cohort.status == CohortStatus.COMPLETE.value
            assert cohort.completed_component_count == 1
            assert cohort.finished_at is not None
            assert len(coverages) == 2
            assert {coverage.comment_scan_run_id for coverage in coverages} == {scan_id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_worker_failure_closes_active_scan_before_aggregation() -> None:
    engine, session_factory = await _database()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            (
                task_id,
                cohort_id,
                component_id,
                scan_id,
            ) = await _materialize_hot_scan_task(
                session,
                now=now,
                bvid="BV-SCAN-FAILED",
                max_retries=0,
            )
        worker = Worker(
            session_factory=session_factory,
            collectors={
                TaskKind.FETCH_HOT_COMMENTS: TerminalFailingScanCollector(now),
            },
            run_id="cohort-scan-failed",
            lease_owner="worker-c4",
        )

        assert await worker.run_once(now=now) is True
        async with session_factory() as session:
            task = await session.get(CollectionTask, task_id)
            scan = await session.get(CommentScanRun, scan_id)
            component = await session.get(SnapshotCohortComponent, component_id)
            cohort = await session.get(SnapshotCohort, cohort_id)
            coverage = await session.scalar(select(CollectionCoverageStat))

            assert task is not None and task.status is TaskStatus.FAILED
            assert scan is not None and scan.status is CommentScanStatus.FAILED
            assert scan.outcome == "retry_exhausted"
            assert scan.pages_requested == 1
            assert scan.pages_succeeded == 0
            assert component is not None
            assert component.status == CohortComponentStatus.FAILED.value
            assert component.requested_pages == 1
            assert component.succeeded_pages == 0
            assert cohort is not None and cohort.status == CohortStatus.PARTIAL.value
            assert coverage is not None
            assert coverage.status == "failed"
            assert coverage.comment_scan_run_id == scan_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_parse_failure_marks_scan_and_cohort_corrupted() -> None:
    engine, session_factory = await _database()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            (
                task_id,
                cohort_id,
                component_id,
                scan_id,
            ) = await _materialize_hot_scan_task(
                session,
                now=now,
                bvid="BV-SCAN-CORRUPTED",
                max_retries=0,
            )
        worker = Worker(
            session_factory=session_factory,
            collectors={
                TaskKind.FETCH_HOT_COMMENTS: ParseFailingScanCollector(now),
            },
            run_id="cohort-scan-corrupted",
            lease_owner="worker-c4",
        )

        assert await worker.run_once(now=now) is True
        async with session_factory() as session:
            task = await session.get(CollectionTask, task_id)
            scan = await session.get(CommentScanRun, scan_id)
            component = await session.get(SnapshotCohortComponent, component_id)
            cohort = await session.get(SnapshotCohort, cohort_id)
            coverage = await session.scalar(select(CollectionCoverageStat))
            raw_payload = await session.scalar(select(RawPayload))

            assert task is not None and task.status is TaskStatus.FAILED
            assert scan is not None and scan.status is CommentScanStatus.CORRUPTED
            assert scan.outcome == "retry_exhausted"
            assert scan.last_error_type == "ParseFailure"
            assert component is not None
            assert component.status == CohortComponentStatus.CORRUPTED.value
            assert component.finished_at is not None
            assert cohort is not None and cohort.status == CohortStatus.CORRUPTED.value
            assert coverage is not None
            assert coverage.reason == "parse_error"
            assert coverage.comment_scan_run_id == scan_id
            assert raw_payload is not None
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
