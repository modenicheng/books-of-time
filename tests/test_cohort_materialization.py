from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.cohort_repositories import (
    CohortComponentPlan,
    SnapshotCohortPlan,
    SnapshotCohortRepository,
)
from books_of_time.db.models import (
    CollectionPolicyVersion,
    CollectionTask,
    CommentScanRun,
    FrontierState,
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
from books_of_time.domain.enums import (
    CommentScanMode,
    CommentScanStatus,
    TaskKind,
    TaskStatus,
)


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_graph(session, *, bvid: str, now: datetime) -> None:
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
            pubdate=now - timedelta(hours=2),
            first_seen_at=now - timedelta(hours=2),
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
            schedule_anchor_at=now - timedelta(hours=2),
            policy_version="cohort-default-v1",
            extra={},
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()


def _routine_plan(now: datetime, *, bvid: str = "BV-C3") -> SnapshotCohortPlan:
    return SnapshotCohortPlan(
        cohort_key=f"snapshot:{bvid}:2026-07-14T04:00:00Z:routine",
        bvid=bvid,
        scheduled_for=now,
        reason="routine",
        age_checkpoint_hours=None,
        desired_tier=CollectionTier.S,
        effective_tier=CollectionTier.S,
        policy_version="cohort-default-v1",
        deadline=now + timedelta(minutes=2),
        status=CohortStatus.PLANNED,
        status_reason=None,
        extra={"planner_bucket_seconds": 30},
        components=(
            CohortComponentPlan(
                "video_metrics",
                TaskKind.FETCH_VIDEO_STATS,
                1,
                priority=102,
            ),
            CohortComponentPlan(
                "hot_core",
                TaskKind.FETCH_HOT_COMMENTS,
                1,
                priority=101,
                payload={"page": 1, "page_limit": 1},
            ),
            CohortComponentPlan(
                "latest_current_head",
                TaskKind.FETCH_LATEST_COMMENTS,
                1,
                priority=100,
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


def _hot_checkpoint_plan(
    now: datetime,
    *,
    bvid: str,
) -> SnapshotCohortPlan:
    def hot_component(
        kind: str,
        start_page: int,
        end_page: int,
        priority: int,
    ) -> CohortComponentPlan:
        target_pages = end_page - start_page + 1
        scan_settings = {
            "scan_mode": kind,
            "start_page": start_page,
            "end_page": end_page,
            "target_pages": target_pages,
            "max_pages_per_slice": 10,
            "max_scan_seconds": 55,
        }
        return CohortComponentPlan(
            kind,
            TaskKind.FETCH_HOT_COMMENTS,
            target_pages,
            priority=priority,
            payload={
                **scan_settings,
                "page": start_page,
                "page_limit": target_pages,
            },
            extra=scan_settings,
        )

    return SnapshotCohortPlan(
        cohort_key=f"snapshot:{bvid}:age:6h",
        bvid=bvid,
        scheduled_for=now,
        reason="age_checkpoint",
        age_checkpoint_hours=6,
        desired_tier=CollectionTier.S,
        effective_tier=CollectionTier.S,
        policy_version="cohort-default-v1",
        deadline=now + timedelta(minutes=60),
        status=CohortStatus.PLANNED,
        status_reason=None,
        extra={"checkpoint_hours": 6},
        components=(
            hot_component("hot_core", 1, 3, 121),
            hot_component("hot_deep", 4, 20, 120),
        ),
    )


def _latest_only_plan(
    now: datetime,
    *,
    bvid: str,
    cohort_suffix: str = "routine",
    component_kind: str = "latest_current_head",
) -> SnapshotCohortPlan:
    return SnapshotCohortPlan(
        cohort_key=f"snapshot:{bvid}:{now.isoformat()}:{cohort_suffix}",
        bvid=bvid,
        scheduled_for=now,
        reason=cohort_suffix,
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
                component_kind,
                TaskKind.FETCH_LATEST_COMMENTS,
                1,
                priority=100,
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


def _frontier(
    now: datetime,
    *,
    bvid: str,
    baseline_status: str,
    frontier_rpid: int | None = None,
    anchors: list[dict[str, object]] | None = None,
    cursor: str | None = None,
    extra: dict[str, object] | None = None,
) -> FrontierState:
    return FrontierState(
        target_type="video",
        target_id=bvid,
        frontier_type="latest_comments",
        frontier_rpid=frontier_rpid,
        frontier_time=None,
        frontier_anchor_set=anchors or [],
        active_scan_run_id=None,
        version=0,
        cursor=cursor,
        last_scan_at=now,
        last_scan_status=baseline_status,
        last_scan_pages=1,
        last_scan_truncated=False,
        extra={"baseline_status": baseline_status, **(extra or {})},
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_shadow_materialization_is_idempotent_and_creates_no_tasks() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        await _seed_graph(session, bvid="BV-C3", now=now)
        repository = SnapshotCohortRepository(session)

        first = await repository.materialize(
            _routine_plan(now),
            rollout_mode=CohortRolloutMode.SHADOW,
            now=now,
        )
        second = await repository.materialize(
            _routine_plan(now),
            rollout_mode=CohortRolloutMode.SHADOW,
            now=now + timedelta(seconds=1),
        )

        assert second.cohort.id == first.cohort.id
        assert first.cohort.status == CohortStatus.SHADOW_PLANNED.value
        assert first.cohort.extra["shadow_target_status"] == CohortStatus.PLANNED.value
        assert first.cohort.extra["rollout_mode"] == CohortRolloutMode.SHADOW.value
        assert first.cohort.expected_component_count == 3
        assert first.cohort_created is True
        assert first.components_created == 3
        assert first.tasks_created == 0
        assert second.cohort_created is False
        assert second.components_created == 0
        assert second.tasks_created == 0
        assert len(second.components) == 3
        assert all(
            component.status == CohortComponentStatus.PENDING.value
            for component in second.components
        )
        assert (
            await session.scalar(select(func.count()).select_from(CollectionTask)) == 0
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_live_materialization_links_tasks_and_never_recreates_initial_tasks() -> (
    None
):
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        await _seed_graph(session, bvid="BV-C3", now=now)
        repository = SnapshotCohortRepository(session)
        first = await repository.materialize(
            _routine_plan(now),
            rollout_mode=CohortRolloutMode.LIVE,
            now=now,
        )

        assert first.cohort.status == CohortStatus.PLANNED.value
        assert first.tasks_created == 3
        assert len(first.tasks) == 3
        assert {task.snapshot_cohort_id for task in first.tasks} == {first.cohort.id}
        assert {task.snapshot_cohort_component_id for task in first.tasks} == {
            component.id for component in first.components
        }
        tasks_by_kind = {task.payload["component_kind"]: task for task in first.tasks}
        for component_kind in {"video_metrics", "hot_core"}:
            assert tasks_by_kind[component_kind].idempotency_key == (
                f"{first.cohort.cohort_key}:{component_kind}"
            )
        latest_task = tasks_by_kind["latest_current_head"]
        assert latest_task.idempotency_key == latest_task.scan_slice_key
        assert latest_task.idempotency_key == (
            f"{latest_task.comment_scan_run_id}:baseline_tail:0"
        )
        assert all(task.payload["bvid"] == "BV-C3" for task in first.tasks)
        assert all(
            task.payload["cohort_key"] == first.cohort.cohort_key
            for task in first.tasks
        )

        for task in first.tasks:
            task.status = TaskStatus.SUCCEEDED
        await session.flush()

        second = await repository.materialize(
            _routine_plan(now),
            rollout_mode=CohortRolloutMode.LIVE,
            now=now + timedelta(minutes=1),
        )

        assert second.tasks_created == 0
        assert len(second.tasks) == 3
        assert (
            await session.scalar(select(func.count()).select_from(CollectionTask)) == 3
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_live_hot_components_materialize_scan_runs_and_slice_zero_once() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)

    async with session_factory() as session:
        await _seed_graph(session, bvid="BV-HOT-LIVE", now=now)
        repository = SnapshotCohortRepository(session)
        plan = _hot_checkpoint_plan(now, bvid="BV-HOT-LIVE")

        first = await repository.materialize(
            plan,
            rollout_mode=CohortRolloutMode.LIVE,
            now=now,
        )
        second = await repository.materialize(
            plan,
            rollout_mode=CohortRolloutMode.LIVE,
            now=now + timedelta(seconds=1),
        )

        scans = list(
            await session.scalars(select(CommentScanRun).order_by(CommentScanRun.mode))
        )
        components = {
            component.component_kind: component for component in first.components
        }
        tasks = {task.payload["component_kind"]: task for task in first.tasks}

        assert first.tasks_created == 2
        assert second.tasks_created == 0
        assert len(scans) == 2
        assert len(second.tasks) == 2
        assert await session.scalar(select(func.count(CollectionTask.id))) == 2
        for scan in scans:
            component = components[scan.mode.value]
            task = tasks[scan.mode.value]
            assert scan.scan_key == f"{plan.cohort_key}:{scan.mode.value}"
            assert scan.snapshot_cohort_id == first.cohort.id
            assert component.comment_scan_run_id == scan.id
            assert task.comment_scan_run_id == scan.id
            assert task.scan_slice_no == 0
            assert task.scan_slice_key == f"{scan.id}:{scan.mode.value}:0"
            assert task.idempotency_key == f"{plan.cohort_key}:{scan.mode.value}"
            assert task.payload["start_page"] == scan.extra["start_page"]
            assert task.payload["end_page"] == scan.extra["end_page"]
            assert task.payload["target_pages"] == scan.target_pages

        core = next(scan for scan in scans if scan.mode.value == "hot_core")
        deep = next(scan for scan in scans if scan.mode.value == "hot_deep")
        assert (core.extra["start_page"], core.extra["end_page"]) == (1, 3)
        assert (deep.extra["start_page"], deep.extra["end_page"]) == (4, 20)

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("baseline_status", "expected_mode", "expected_anchor_rpid"),
    [
        (None, CommentScanMode.BASELINE_TAIL, None),
        ("baseline_tail_complete", CommentScanMode.BASELINE_HEAD_SWEEP, 1001),
        ("baseline_complete", CommentScanMode.INCREMENTAL, 2001),
    ],
)
async def test_live_latest_component_claims_mode_and_slice_zero(
    baseline_status: str | None,
    expected_mode: CommentScanMode,
    expected_anchor_rpid: int | None,
) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    bvid = f"BV-LATEST-{expected_mode.value}"

    async with session_factory() as session:
        await _seed_graph(session, bvid=bvid, now=now)
        if baseline_status == "baseline_tail_complete":
            session.add(
                _frontier(
                    now,
                    bvid=bvid,
                    baseline_status=baseline_status,
                    cursor="",
                    extra={"baseline_start_frontier_rpid": 1001},
                )
            )
        elif baseline_status == "baseline_complete":
            session.add(
                _frontier(
                    now,
                    bvid=bvid,
                    baseline_status=baseline_status,
                    frontier_rpid=2001,
                    anchors=[{"rpid": 2001, "platform_created_at": None}],
                )
            )
        await session.flush()

        result = await SnapshotCohortRepository(session).materialize(
            _latest_only_plan(now, bvid=bvid),
            rollout_mode=CohortRolloutMode.LIVE,
            now=now,
        )

        scan = await session.scalar(select(CommentScanRun))
        frontier = await session.scalar(select(FrontierState))
        assert scan is not None
        assert frontier is not None
        assert len(result.tasks) == 1
        task = result.tasks[0]
        component = result.components[0]
        assert result.tasks_created == 1
        assert scan.mode is expected_mode
        assert scan.status is CommentScanStatus.PLANNED
        assert scan.start_frontier_rpid == expected_anchor_rpid
        assert [item["rpid"] for item in scan.start_anchor_set] == (
            [] if expected_anchor_rpid is None else [expected_anchor_rpid]
        )
        assert frontier.active_scan_run_id == scan.id
        assert frontier.version == 1
        assert component.comment_scan_run_id == scan.id
        assert component.status == CohortComponentStatus.PENDING.value
        assert task.comment_scan_run_id == scan.id
        assert task.scan_slice_no == 0
        assert task.scan_slice_key == f"{scan.id}:{expected_mode.value}:0"
        assert task.idempotency_key == task.scan_slice_key
        assert task.payload["scan_mode"] == expected_mode.value
        assert task.payload["frontier_version"] == frontier.version
        assert task.payload["max_scan_seconds"] == 48
        assert task.payload["current_head_required"] is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_second_latest_cohort_joins_active_scan_without_duplicate_task() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    bvid = "BV-LATEST-JOIN"

    async with session_factory() as session:
        await _seed_graph(session, bvid=bvid, now=now)
        repository = SnapshotCohortRepository(session)
        first_plan = _latest_only_plan(now, bvid=bvid)
        first = await repository.materialize(
            first_plan,
            rollout_mode=CohortRolloutMode.LIVE,
            now=now,
        )
        repeated = await repository.materialize(
            first_plan,
            rollout_mode=CohortRolloutMode.LIVE,
            now=now + timedelta(seconds=1),
        )
        second = await repository.materialize(
            _latest_only_plan(
                now + timedelta(seconds=30),
                bvid=bvid,
                cohort_suffix="checkpoint_recovery",
                component_kind="latest_reconciliation",
            ),
            rollout_mode=CohortRolloutMode.LIVE,
            now=now + timedelta(seconds=30),
        )

        assert first.tasks_created == 1
        assert repeated.tasks_created == 0
        assert len(repeated.tasks) == 1
        assert repeated.tasks[0].id == first.tasks[0].id
        assert second.tasks_created == 0
        assert second.tasks == ()
        assert second.components[0].status == (
            CohortComponentStatus.JOINED_ACTIVE_TASK.value
        )
        assert second.components[0].comment_scan_run_id == (
            first.components[0].comment_scan_run_id
        )
        assert await session.scalar(select(func.count(CommentScanRun.id))) == 1
        assert await session.scalar(select(func.count(CollectionTask.id))) == 1
        frontier = await session.scalar(select(FrontierState))
        assert frontier is not None
        assert frontier.version == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_repeated_latest_materialization_keeps_terminal_scan_and_task() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    bvid = "BV-LATEST-TERMINAL-IDEMPOTENT"

    async with session_factory() as session:
        await _seed_graph(session, bvid=bvid, now=now)
        repository = SnapshotCohortRepository(session)
        plan = _latest_only_plan(now, bvid=bvid)
        first = await repository.materialize(
            plan,
            rollout_mode=CohortRolloutMode.LIVE,
            now=now,
        )
        scan = await session.get(
            CommentScanRun,
            first.components[0].comment_scan_run_id,
        )
        assert scan is not None
        scan.status = CommentScanStatus.COMPLETE
        scan.outcome = "frontier_reached"
        scan.finished_at = now + timedelta(seconds=10)
        first.tasks[0].status = TaskStatus.SUCCEEDED
        await session.flush()

        repeated = await repository.materialize(
            plan,
            rollout_mode=CohortRolloutMode.LIVE,
            now=now + timedelta(seconds=30),
        )

        assert repeated.tasks_created == 0
        assert len(repeated.tasks) == 1
        assert repeated.tasks[0].id == first.tasks[0].id
        assert repeated.components[0].comment_scan_run_id == scan.id
        assert await session.scalar(select(func.count(CommentScanRun.id))) == 1
        assert await session.scalar(select(func.count(CollectionTask.id))) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_latest_component_creates_no_frontier_scan_or_task() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    bvid = "BV-LATEST-SHADOW"

    async with session_factory() as session:
        await _seed_graph(session, bvid=bvid, now=now)
        result = await SnapshotCohortRepository(session).materialize(
            _latest_only_plan(now, bvid=bvid),
            rollout_mode=CohortRolloutMode.SHADOW,
            now=now,
        )

        assert result.tasks_created == 0
        assert result.components[0].comment_scan_run_id is None
        assert await session.scalar(select(func.count(FrontierState.id))) == 0
        assert await session.scalar(select(func.count(CommentScanRun.id))) == 0
        assert await session.scalar(select(func.count(CollectionTask.id))) == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_shadow_hot_components_create_plans_without_runs_or_tasks() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)

    async with session_factory() as session:
        await _seed_graph(session, bvid="BV-HOT-SHADOW", now=now)
        result = await SnapshotCohortRepository(session).materialize(
            _hot_checkpoint_plan(now, bvid="BV-HOT-SHADOW"),
            rollout_mode=CohortRolloutMode.SHADOW,
            now=now,
        )

        assert {component.planned_pages for component in result.components} == {3, 17}
        assert result.tasks_created == 0
        assert await session.scalar(select(func.count(CommentScanRun.id))) == 0
        assert await session.scalar(select(func.count(CollectionTask.id))) == 0
        assert all(
            component.comment_scan_run_id is None for component in result.components
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_materialization_rejects_conflicting_stable_key_identity() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        await _seed_graph(session, bvid="BV-C3", now=now)
        repository = SnapshotCohortRepository(session)
        plan = _routine_plan(now)
        await repository.materialize(
            plan,
            rollout_mode=CohortRolloutMode.SHADOW,
            now=now,
        )
        await session.commit()

        conflicting = replace(
            plan,
            scheduled_for=now + timedelta(seconds=30),
        )
        with pytest.raises(ValueError, match="cohort key identity conflict"):
            await repository.materialize(
                conflicting,
                rollout_mode=CohortRolloutMode.SHADOW,
                now=now + timedelta(seconds=30),
            )
        await session.rollback()

    async with session_factory() as session:
        cohort = await session.scalar(select(SnapshotCohort))
        components = list(await session.scalars(select(SnapshotCohortComponent)))
        assert cohort is not None
        assert cohort.scheduled_for == now
        assert len(components) == 3

    await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_materialization_can_only_add_new_missing_components() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        await _seed_graph(session, bvid="BV-C3", now=now)
        repository = SnapshotCohortRepository(session)
        base = SnapshotCohortPlan(
            cohort_key="snapshot:BV-C3:recovery:through:6h",
            bvid="BV-C3",
            scheduled_for=now,
            reason="recovery",
            age_checkpoint_hours=None,
            desired_tier=CollectionTier.S,
            effective_tier=CollectionTier.S,
            policy_version="cohort-default-v1",
            deadline=now + timedelta(minutes=2),
            status=CohortStatus.PLANNED,
            status_reason="checkpoint_recovery",
            extra={"latest_overdue_hours": 6},
            components=(
                CohortComponentPlan(
                    "video_metrics",
                    TaskKind.FETCH_VIDEO_STATS,
                    1,
                ),
            ),
        )
        first = await repository.materialize(
            base,
            rollout_mode=CohortRolloutMode.LIVE,
            now=now,
        )
        extended = replace(
            base,
            components=(
                *base.components,
                CohortComponentPlan(
                    "hot_core",
                    TaskKind.FETCH_HOT_COMMENTS,
                    1,
                    payload={"page": 1, "page_limit": 1},
                ),
            ),
        )
        second = await repository.materialize(
            extended,
            rollout_mode=CohortRolloutMode.LIVE,
            now=now + timedelta(seconds=30),
        )

        assert first.cohort.id == second.cohort.id
        assert second.components_created == 1
        assert second.tasks_created == 1
        assert second.cohort.expected_component_count == 2

        conflicting_component = replace(
            extended,
            components=(
                CohortComponentPlan(
                    "video_metrics",
                    TaskKind.FETCH_VIDEO_STATS,
                    2,
                ),
                extended.components[1],
            ),
        )
        with pytest.raises(ValueError, match="component plan conflict"):
            await repository.materialize(
                conflicting_component,
                rollout_mode=CohortRolloutMode.LIVE,
                now=now + timedelta(minutes=1),
            )

    await engine.dispose()


@pytest.mark.asyncio
async def test_materialization_flushes_without_committing() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        await _seed_graph(session, bvid="BV-C3", now=now)
        await SnapshotCohortRepository(session).materialize(
            _routine_plan(now),
            rollout_mode=CohortRolloutMode.LIVE,
            now=now,
        )
        await session.rollback()

    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(SnapshotCohort)) == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(CollectionTask)) == 0
        )

    await engine.dispose()
