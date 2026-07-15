from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.cohort_repositories import (
    CohortComponentPlan,
    CollectionPolicyVersionRepository,
    SnapshotCohortPlan,
    SnapshotCohortRepository,
)
from books_of_time.db.models import (
    CollectionPolicyVersion,
    CollectionScheduleGap,
    CollectionTask,
    CommentScanRun,
    FrontierState,
    KnownVideo,
    KnownVideoSource,
    SnapshotCohort,
    SnapshotCohortComponent,
    VideoCollectionState,
    VideoMetricSnapshot,
)
from books_of_time.db.repositories import (
    FrontierStateRepository,
    FrontierStateUpdate,
)
from books_of_time.domain.cohort_policy import (
    CohortComponentStatus,
    CohortPolicy,
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
from books_of_time.task_orchestrator.snapshot_cohort_planner import (
    SnapshotCohortPlanner,
    _component_plans_for_kinds,
    _hot_component_plans,
    _prefer_recovery_component_plan,
)


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _policy(**overrides) -> CohortPolicy:
    return CohortPolicy.from_config(
        {
            "snapshot_cohorts": {
                "enabled": True,
                "policy_version": "cohort-default-v1",
                "rollout_mode": "shadow",
                **overrides,
            }
        }
    )


async def _seed_video(
    session,
    *,
    bvid: str,
    pubdate: datetime,
    first_seen_at: datetime | None = None,
    monitored_official: bool = False,
) -> KnownVideo:
    first_seen = first_seen_at or pubdate
    video = KnownVideo(
        bvid=bvid,
        source_mid="42",
        pubdate=pubdate,
        first_seen_at=first_seen,
        created_at=first_seen,
        updated_at=first_seen,
    )
    session.add(video)
    await session.flush()
    if monitored_official:
        session.add(
            KnownVideoSource(
                bvid=bvid,
                source_mid="42",
                pool_type="game",
                pool_id="test-game",
                game_id="test-game",
                official=True,
                monitored=True,
                first_seen_at=first_seen,
                last_seen_at=first_seen,
                active=True,
                created_at=first_seen,
                updated_at=first_seen,
            )
        )
        await session.flush()
    return video


async def _cohorts(session, bvid: str) -> list[SnapshotCohort]:
    return list(
        await session.scalars(
            select(SnapshotCohort)
            .where(SnapshotCohort.bvid == bvid)
            .order_by(SnapshotCohort.scheduled_for, SnapshotCohort.id)
        )
    )


async def _materialize_latest_for_repair(
    session,
    *,
    policy: CohortPolicy,
    bvid: str,
    now: datetime,
    deadline: datetime,
    baseline_complete: bool,
    rollout_mode: CohortRolloutMode = CohortRolloutMode.LIVE,
    priority: int = 100,
    budget_cost: int = 1,
    max_retries: int = 3,
):
    await CollectionPolicyVersionRepository(session).ensure_configured(
        policy,
        now=now,
    )
    await _seed_video(
        session,
        bvid=bvid,
        pubdate=now - timedelta(hours=1),
    )
    session.add(
        VideoCollectionState(
            bvid=bvid,
            desired_tier=CollectionTier.S.value,
            effective_tier=CollectionTier.S.value,
            candidate_downgrade_tier=None,
            consecutive_downgrade_count=0,
            pinned_tier=CollectionTier.S.value,
            life_stage="active",
            schedule_anchor_at=now - timedelta(hours=1),
            next_due_at=now + timedelta(hours=1),
            last_planned_at=now,
            last_completed_cohort_at=None,
            last_checkpoint_hours=None,
            policy_version=policy.policy_version,
            extra={},
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    if baseline_complete:
        frontier_repository = FrontierStateRepository(session)
        frontier = await frontier_repository.get_or_create(
            target_type="video",
            target_id=bvid,
            frontier_type="latest_comments",
            now=now,
        )
        frontier.frontier_rpid = 8001
        frontier.frontier_time = now - timedelta(minutes=1)
        frontier.frontier_anchor_set = [
            {
                "rpid": 8001,
                "platform_created_at": (now - timedelta(minutes=1)).isoformat(),
            }
        ]
        frontier.extra = {"baseline_status": "baseline_complete"}
        await frontier_repository.save(frontier)

    plan = SnapshotCohortPlan(
        cohort_key=f"snapshot:{bvid}:repair",
        bvid=bvid,
        scheduled_for=now,
        reason="routine",
        age_checkpoint_hours=None,
        desired_tier=CollectionTier.S,
        effective_tier=CollectionTier.S,
        policy_version=policy.policy_version,
        deadline=deadline,
        status=CohortStatus.PLANNED,
        status_reason=None,
        extra={},
        components=(
            CohortComponentPlan(
                "latest_current_head",
                TaskKind.FETCH_LATEST_COMMENTS,
                1,
                priority=priority,
                budget_cost=budget_cost,
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
    return await SnapshotCohortRepository(session).materialize(
        plan,
        rollout_mode=rollout_mode,
        now=now,
    )


@pytest.mark.asyncio
async def test_live_planner_repair_ignores_shadow_latest_components() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    policy = _policy(rollout_mode="live")
    try:
        async with session_factory.begin() as session:
            shadow = await _materialize_latest_for_repair(
                session,
                policy=policy,
                bvid="BV-LATEST-SHADOW-REPAIR",
                now=now,
                deadline=now + timedelta(minutes=5),
                baseline_complete=False,
                rollout_mode=CohortRolloutMode.SHADOW,
            )

            await SnapshotCohortPlanner(policy).plan_due(
                session,
                now=now + timedelta(seconds=30),
                rollout_mode=CohortRolloutMode.LIVE,
            )

            cohort = await session.get(SnapshotCohort, shadow.cohort.id)
            component = await session.get(
                SnapshotCohortComponent,
                shadow.components[0].id,
            )
            assert cohort is not None
            assert cohort.status == CohortStatus.SHADOW_PLANNED.value
            assert component is not None
            assert component.status == CohortComponentStatus.PENDING.value
            assert component.comment_scan_run_id is None
            assert await session.scalar(select(func.count(CommentScanRun.id))) == 0
            assert await session.scalar(select(func.count(CollectionTask.id))) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_planner_finalizes_expired_latest_consumers_by_scan_phase() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    policy = _policy(rollout_mode="live")
    try:
        async with session_factory.begin() as session:
            tail = await _materialize_latest_for_repair(
                session,
                policy=policy,
                bvid="BV-LATEST-DEADLINE-TAIL",
                now=now,
                deadline=now + timedelta(seconds=20),
                baseline_complete=False,
            )
            incremental = await _materialize_latest_for_repair(
                session,
                policy=policy,
                bvid="BV-LATEST-DEADLINE-INCREMENTAL",
                now=now,
                deadline=now + timedelta(seconds=20),
                baseline_complete=True,
            )

            await SnapshotCohortPlanner(policy).plan_due(
                session,
                now=now + timedelta(seconds=20),
                rollout_mode=CohortRolloutMode.LIVE,
            )

            tail_component = await session.get(
                SnapshotCohortComponent,
                tail.components[0].id,
            )
            incremental_component = await session.get(
                SnapshotCohortComponent,
                incremental.components[0].id,
            )
            tail_cohort = await session.get(SnapshotCohort, tail.cohort.id)
            incremental_cohort = await session.get(
                SnapshotCohort,
                incremental.cohort.id,
            )

            assert tail_component is not None
            assert tail_component.status == CohortComponentStatus.PARTIAL.value
            assert tail_component.failure_reason == "baseline_tail_in_progress"
            assert incremental_component is not None
            assert incremental_component.status == CohortComponentStatus.PARTIAL.value
            assert incremental_component.failure_reason == "current_head_not_captured"
            assert tail_cohort is not None
            assert tail_cohort.status == CohortStatus.PARTIAL.value
            assert incremental_cohort is not None
            assert incremental_cohort.status == CohortStatus.PARTIAL.value
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_planner_repairs_latest_tasks_and_rebinds_terminal_scan() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    policy = _policy(rollout_mode="live")
    bvid = "BV-LATEST-PLANNER-REPAIR"
    try:
        async with session_factory.begin() as session:
            materialized = await _materialize_latest_for_repair(
                session,
                policy=policy,
                bvid=bvid,
                now=now,
                deadline=now + timedelta(minutes=5),
                baseline_complete=True,
                priority=117,
                budget_cost=4,
                max_retries=6,
            )
            original_scan_id = materialized.components[0].comment_scan_run_id
            assert original_scan_id is not None
            await session.delete(materialized.tasks[0])
            await session.flush()

            planner = SnapshotCohortPlanner(policy)
            await planner.plan_due(
                session,
                now=now + timedelta(seconds=30),
                rollout_mode=CohortRolloutMode.LIVE,
            )

            repaired_task = await session.scalar(
                select(CollectionTask).where(
                    CollectionTask.comment_scan_run_id == original_scan_id
                )
            )
            assert repaired_task is not None
            assert repaired_task.scan_slice_no == 0
            assert repaired_task.scan_slice_key == (f"{original_scan_id}:incremental:0")
            assert repaired_task.priority == 117
            assert repaired_task.budget_cost == 4
            assert repaired_task.max_retries == 6

            original_scan = await session.get(CommentScanRun, original_scan_id)
            frontier = await session.scalar(
                select(FrontierState).where(FrontierState.target_id == bvid)
            )
            assert original_scan is not None
            assert frontier is not None
            original_scan.status = CommentScanStatus.COMPLETE
            original_scan.outcome = "frontier_reached"
            original_scan.finished_at = now + timedelta(seconds=40)
            repaired_task.status = TaskStatus.SUCCEEDED
            await FrontierStateRepository(session).compare_and_swap(
                frontier.id,
                frontier.version,
                FrontierStateUpdate(
                    frontier_rpid=frontier.frontier_rpid,
                    frontier_time=frontier.frontier_time,
                    frontier_anchor_set=frontier.frontier_anchor_set,
                    active_scan_run_id=None,
                    cursor=None,
                    last_scan_at=now + timedelta(seconds=40),
                    last_scan_status=CommentScanStatus.COMPLETE.value,
                    last_scan_pages=0,
                    last_scan_truncated=False,
                    extra=frontier.extra,
                ),
                now=now + timedelta(seconds=40),
            )

            await planner.plan_due(
                session,
                now=now + timedelta(seconds=60),
                rollout_mode=CohortRolloutMode.LIVE,
            )

            component = await session.get(
                SnapshotCohortComponent,
                materialized.components[0].id,
            )
            scans = list(
                await session.scalars(
                    select(CommentScanRun)
                    .where(CommentScanRun.bvid == bvid)
                    .order_by(CommentScanRun.id)
                )
            )
            tasks = list(
                await session.scalars(
                    select(CollectionTask)
                    .where(CollectionTask.target_id == bvid)
                    .order_by(CollectionTask.id)
                )
            )
            frontier = await session.scalar(
                select(FrontierState).where(FrontierState.target_id == bvid)
            )

            assert component is not None
            assert len(scans) == 2
            assert scans[1].mode is CommentScanMode.INCREMENTAL
            assert scans[1].status is CommentScanStatus.PLANNED
            assert component.comment_scan_run_id == scans[1].id
            assert component.status == CohortComponentStatus.JOINED_ACTIVE_TASK.value
            assert frontier is not None
            assert frontier.active_scan_run_id == scans[1].id
            assert len(tasks) == 2
            assert tasks[1].comment_scan_run_id == scans[1].id
            assert tasks[1].scan_slice_key == f"{scans[1].id}:incremental:0"
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("tier", "routine_pages", "checkpoint_ranges"),
    [
        (CollectionTier.S, 3, (("hot_core", 1, 3), ("hot_deep", 4, 20))),
        (CollectionTier.A, 2, (("hot_core", 1, 2), ("hot_deep", 3, 10))),
        (CollectionTier.B, 1, (("hot_core", 1, 1), ("hot_deep", 2, 3))),
        (CollectionTier.C, 1, (("hot_core", 1, 1),)),
    ],
)
def test_hot_component_plan_matrix(
    tier: CollectionTier,
    routine_pages: int,
    checkpoint_ranges: tuple[tuple[str, int, int], ...],
) -> None:
    policy = _policy(policy_version="cohort-default-v2")

    def priority_for(kind: str) -> int:
        return 101 if kind == "hot_core" else 100

    routine = _hot_component_plans(
        policy,
        tier,
        include_deep=False,
        dormant=False,
        status=CohortComponentStatus.PENDING,
        priority_for=priority_for,
    )
    checkpoint = _hot_component_plans(
        policy,
        tier,
        include_deep=True,
        dormant=False,
        status=CohortComponentStatus.PENDING,
        priority_for=priority_for,
    )

    assert [(plan.component_kind, plan.planned_pages) for plan in routine] == [
        ("hot_core", routine_pages)
    ]
    assert [
        (
            plan.component_kind,
            plan.extra["start_page"],
            plan.extra["end_page"],
        )
        for plan in checkpoint
    ] == list(checkpoint_ranges)
    for plan in (*routine, *checkpoint):
        assert plan.extra == {
            "scan_mode": plan.component_kind,
            "start_page": plan.payload["start_page"],
            "end_page": plan.payload["end_page"],
            "target_pages": plan.planned_pages,
            "max_pages_per_slice": 10,
            "max_scan_seconds": 55,
        }
        assert plan.payload["page"] == plan.extra["start_page"]
        assert plan.payload["page_limit"] == plan.planned_pages


def test_dormant_hot_plan_is_one_core_page_and_no_deep() -> None:
    plans = _hot_component_plans(
        _policy(policy_version="cohort-default-v2"),
        CollectionTier.S,
        include_deep=True,
        dormant=True,
        status=CohortComponentStatus.PENDING,
        priority_for=lambda _kind: 100,
    )

    assert len(plans) == 1
    assert plans[0].component_kind == "hot_core"
    assert plans[0].planned_pages == 1
    assert plans[0].extra["start_page"] == 1
    assert plans[0].extra["end_page"] == 1


@pytest.mark.parametrize(
    ("interval_seconds", "expected_slice_seconds"),
    [
        (60, 24),
        (120, 48),
        (138, 55),
        (600, 55),
        (None, 55),
    ],
)
def test_latest_component_slice_budget_tracks_routine_interval(
    interval_seconds: int | None,
    expected_slice_seconds: int,
) -> None:
    plans = _component_plans_for_kinds(
        ("latest_current_head", "latest_reconciliation"),
        policy=_policy(policy_version="cohort-default-v2"),
        tier=CollectionTier.S,
        include_hot_deep=False,
        dormant=False,
        status=CohortComponentStatus.PENDING,
        priority_for=lambda _kind: 100,
        latest_interval_seconds=interval_seconds,
    )

    assert len(plans) == 2
    for plan in plans:
        assert plan.payload["max_scan_seconds"] == expected_slice_seconds
        assert plan.payload["current_head_required"] is True
        assert plan.extra == {
            "max_scan_seconds": expected_slice_seconds,
            "current_head_required": True,
        }


def test_recovery_prefers_larger_persisted_hot_range_without_recalculation() -> None:
    policy = _policy(policy_version="cohort-default-v2")

    def priority_for(_kind: str) -> int:
        return 100

    a_deep = _hot_component_plans(
        policy,
        CollectionTier.A,
        include_deep=True,
        dormant=False,
        status=CohortComponentStatus.PENDING,
        priority_for=priority_for,
    )[1]
    s_deep = _hot_component_plans(
        policy,
        CollectionTier.S,
        include_deep=True,
        dormant=False,
        status=CohortComponentStatus.PENDING,
        priority_for=priority_for,
    )[1]

    preferred = _prefer_recovery_component_plan(a_deep, s_deep)
    assert preferred.extra["start_page"] == 4
    assert preferred.extra["end_page"] == 20
    assert preferred.planned_pages == 17


@pytest.mark.asyncio
async def test_first_active_s_adoption_plans_checkpoint_depth_in_shadow() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        await _seed_video(
            session,
            bvid="BV-FIRST-S",
            pubdate=now - timedelta(hours=2),
            monitored_official=True,
        )
        await SnapshotCohortPlanner(
            _policy(policy_version="cohort-default-v2")
        ).plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )

        cohort = (await _cohorts(session, "BV-FIRST-S"))[0]
        components = {
            component.component_kind: component
            for component in await session.scalars(
                select(SnapshotCohortComponent).where(
                    SnapshotCohortComponent.cohort_id == cohort.id
                )
            )
        }
        assert components["hot_core"].planned_pages == 3
        assert components["hot_core"].extra["end_page"] == 3
        assert components["hot_deep"].planned_pages == 17
        assert components["hot_deep"].extra["start_page"] == 4
        assert components["hot_deep"].extra["end_page"] == 20
        assert await session.scalar(select(func.count(CommentScanRun.id))) == 0
        assert await session.scalar(select(func.count(CollectionTask.id))) == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_existing_checkpoint_keeps_its_frozen_hot_tier_ranges() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    policy = _policy(policy_version="cohort-default-v2")

    async with session_factory() as session:
        await _seed_video(
            session,
            bvid="BV-FROZEN-HOT",
            pubdate=now - timedelta(hours=6),
        )
        session.add(
            VideoCollectionState(
                bvid="BV-FROZEN-HOT",
                desired_tier=CollectionTier.S.value,
                effective_tier=CollectionTier.S.value,
                candidate_downgrade_tier=None,
                consecutive_downgrade_count=0,
                pinned_tier=CollectionTier.S.value,
                life_stage="active",
                schedule_anchor_at=now - timedelta(hours=6),
                next_due_at=now,
                last_planned_at=None,
                last_completed_cohort_at=None,
                last_checkpoint_hours=None,
                policy_version="cohort-default-v2",
                extra={},
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        planner = SnapshotCohortPlanner(policy)
        await planner.plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )
        state = await session.get(VideoCollectionState, "BV-FROZEN-HOT")
        assert state is not None
        state.pinned_tier = CollectionTier.C.value
        state.desired_tier = CollectionTier.C.value
        state.effective_tier = CollectionTier.C.value
        await session.flush()

        await planner.plan_due(
            session,
            now=now + timedelta(seconds=30),
            rollout_mode=CohortRolloutMode.SHADOW,
        )

        checkpoint = next(
            cohort
            for cohort in await _cohorts(session, "BV-FROZEN-HOT")
            if cohort.reason == "age_checkpoint"
        )
        components = {
            component.component_kind: component
            for component in await session.scalars(
                select(SnapshotCohortComponent).where(
                    SnapshotCohortComponent.cohort_id == checkpoint.id
                )
            )
        }
        assert checkpoint.effective_tier == CollectionTier.S.value
        assert components["hot_core"].planned_pages == 3
        assert components["hot_deep"].planned_pages == 17
        assert components["hot_deep"].extra["end_page"] == 20

    await engine.dispose()


@pytest.mark.asyncio
async def test_first_planning_adopts_video_and_writes_only_current_shadow_routine() -> (
    None
):
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 4, 0, 17, tzinfo=UTC)
    pubdate = now - timedelta(hours=2)

    async with session_factory() as session:
        await _seed_video(session, bvid="BV-FIRST", pubdate=pubdate)
        summary = await SnapshotCohortPlanner(_policy()).plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )

        state = await session.get(VideoCollectionState, "BV-FIRST")
        policy_row = await session.scalar(select(CollectionPolicyVersion))
        cohorts = await _cohorts(session, "BV-FIRST")

        assert summary.videos_considered == 1
        assert summary.videos_adopted == 1
        assert summary.routine_cohorts_created == 1
        assert summary.checkpoint_cohorts_created == 0
        assert summary.tasks_created == 0
        assert state is not None
        assert state.schedule_anchor_at == pubdate
        assert state.last_planned_at == now
        assert state.next_due_at is not None and state.next_due_at > now
        assert policy_row is not None
        assert policy_row.version == "cohort-default-v1"
        assert policy_row.active is True
        assert policy_row.policy == _policy().as_persisted_policy()
        assert len(cohorts) == 1
        assert cohorts[0].reason == "routine"
        assert cohorts[0].status == CohortStatus.SHADOW_PLANNED.value
        assert cohorts[0].scheduled_for == now.replace(second=0, microsecond=0)
        assert (
            await session.scalar(select(func.count()).select_from(CollectionTask)) == 0
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_official_initial_s_uses_publish_age_not_discovery_age() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        await _seed_video(
            session,
            bvid="BV-OFFICIAL-NEW",
            pubdate=now - timedelta(hours=5, minutes=59),
            monitored_official=True,
        )
        await _seed_video(
            session,
            bvid="BV-OFFICIAL-LATE",
            pubdate=now - timedelta(hours=8),
            first_seen_at=now - timedelta(minutes=5),
            monitored_official=True,
        )
        await SnapshotCohortPlanner(_policy()).plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )

        new_state = await session.get(VideoCollectionState, "BV-OFFICIAL-NEW")
        late_state = await session.get(VideoCollectionState, "BV-OFFICIAL-LATE")
        assert new_state is not None and new_state.effective_tier == "s"
        assert late_state is not None and late_state.effective_tier == "c"

    await engine.dispose()


@pytest.mark.asyncio
async def test_due_checkpoint_coalesces_same_bucket_routine() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 6, 0, 12, tzinfo=UTC)
    pubdate = now - timedelta(hours=6)

    async with session_factory() as session:
        await _seed_video(session, bvid="BV-CHECKPOINT", pubdate=pubdate)
        summary = await SnapshotCohortPlanner(_policy()).plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )

        cohorts = await _cohorts(session, "BV-CHECKPOINT")
        assert summary.checkpoint_cohorts_created == 1
        assert summary.routine_cohorts_created == 0
        assert len(cohorts) == 1
        checkpoint = cohorts[0]
        assert checkpoint.reason == "age_checkpoint"
        assert checkpoint.age_checkpoint_hours == 6
        assert checkpoint.scheduled_for == pubdate + timedelta(hours=6)
        assert checkpoint.deadline == checkpoint.scheduled_for + timedelta(minutes=60)
        assert checkpoint.extra["coalesced_routine_bucket"] is True
        assert checkpoint.extra["shadow_target_status"] == CohortStatus.PLANNED.value
        components = list(
            await session.scalars(
                select(SnapshotCohortComponent)
                .where(SnapshotCohortComponent.cohort_id == checkpoint.id)
                .order_by(SnapshotCohortComponent.component_kind)
            )
        )
        assert {component.component_kind for component in components} == {
            "video_metrics",
            "hot_core",
            "latest_reconciliation",
        }

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age", "checkpoint_target", "recovery_count"),
    [
        (timedelta(hours=7), CohortStatus.PLANNED.value, 0),
        (timedelta(hours=7, seconds=1), CohortStatus.MISSED.value, 1),
    ],
)
async def test_checkpoint_lateness_boundary_is_inclusive(
    age: timedelta,
    checkpoint_target: str,
    recovery_count: int,
) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        await _seed_video(session, bvid="BV-LATE", pubdate=now - age)
        await SnapshotCohortPlanner(_policy()).plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )

        cohorts = await _cohorts(session, "BV-LATE")
        checkpoint = next(row for row in cohorts if row.reason == "age_checkpoint")
        recoveries = [row for row in cohorts if row.reason == "recovery"]
        assert checkpoint.extra["shadow_target_status"] == checkpoint_target
        assert len(recoveries) == recovery_count
        if recoveries:
            assert recoveries[0].cohort_key.endswith("recovery:through:6h")

    await engine.dispose()


@pytest.mark.asyncio
async def test_checkpoint_before_first_discovery_is_not_applicable_or_recovered() -> (
    None
):
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    pubdate = now - timedelta(hours=8)

    async with session_factory() as session:
        await _seed_video(
            session,
            bvid="BV-DISCOVERED-LATE",
            pubdate=pubdate,
            first_seen_at=now - timedelta(hours=1),
        )
        await SnapshotCohortPlanner(_policy()).plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )

        cohorts = await _cohorts(session, "BV-DISCOVERED-LATE")
        checkpoint = next(row for row in cohorts if row.reason == "age_checkpoint")
        assert checkpoint.status_reason == "not_applicable_before_discovery"
        assert (
            checkpoint.extra["shadow_target_status"]
            == CohortStatus.NOT_APPLICABLE.value
        )
        assert all(row.reason != "recovery" for row in cohorts)

    await engine.dispose()


@pytest.mark.asyncio
async def test_overdue_checkpoints_collapse_into_idempotent_latest_recovery() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 13, 1, 1, tzinfo=UTC)
    pubdate = now - timedelta(hours=13, minutes=1, seconds=1)
    planner = SnapshotCohortPlanner(_policy())

    async with session_factory() as session:
        await _seed_video(session, bvid="BV-RECOVERY", pubdate=pubdate)
        first = await planner.plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )
        counts_after_first = {
            "cohorts": await session.scalar(
                select(func.count()).select_from(SnapshotCohort)
            ),
            "components": await session.scalar(
                select(func.count()).select_from(SnapshotCohortComponent)
            ),
        }
        second = await planner.plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )
        counts_after_second = {
            "cohorts": await session.scalar(
                select(func.count()).select_from(SnapshotCohort)
            ),
            "components": await session.scalar(
                select(func.count()).select_from(SnapshotCohortComponent)
            ),
        }

        cohorts = await _cohorts(session, "BV-RECOVERY")
        recovery = next(row for row in cohorts if row.reason == "recovery")
        assert recovery.cohort_key.endswith("recovery:through:12h")
        assert first.recovery_cohorts_created == 1
        assert second.cohorts_created == 0
        assert counts_after_second == counts_after_first

    await engine.dispose()


@pytest.mark.asyncio
async def test_new_recovery_coalesces_same_cycle_current_routine() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 7, 0, 1, tzinfo=UTC)
    pubdate = now - timedelta(hours=7, seconds=1)

    async with session_factory() as session:
        await _seed_video(session, bvid="BV-RECOVERY-ROUTINE", pubdate=pubdate)
        summary = await SnapshotCohortPlanner(_policy(rollout_mode="live")).plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.LIVE,
        )

        cohorts = await _cohorts(session, "BV-RECOVERY-ROUTINE")
        tasks = list(await session.scalars(select(CollectionTask)))
        assert summary.recovery_cohorts_created == 1
        assert summary.routine_cohorts_created == 0
        assert {cohort.reason for cohort in cohorts} == {
            "age_checkpoint",
            "recovery",
        }
        assert len(tasks) == 3
        assert {task.payload["component_kind"] for task in tasks} == {
            "video_metrics",
            "hot_core",
            "latest_reconciliation",
        }

    await engine.dispose()


@pytest.mark.asyncio
async def test_live_pending_checkpoint_expires_as_capacity_miss_before_recovery() -> (
    None
):
    engine, session_factory = await _database()
    checkpoint_at = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    pubdate = checkpoint_at - timedelta(hours=6)
    planner = SnapshotCohortPlanner(_policy(rollout_mode="live"))

    async with session_factory() as session:
        await _seed_video(session, bvid="BV-CAPACITY", pubdate=pubdate)
        await planner.plan_due(
            session,
            now=checkpoint_at,
            rollout_mode=CohortRolloutMode.LIVE,
        )
        await planner.plan_due(
            session,
            now=checkpoint_at + timedelta(minutes=60, seconds=1),
            rollout_mode=CohortRolloutMode.LIVE,
        )

        cohorts = await _cohorts(session, "BV-CAPACITY")
        checkpoint = next(row for row in cohorts if row.reason == "age_checkpoint")
        recovery = next(row for row in cohorts if row.reason == "recovery")
        components = list(
            await session.scalars(
                select(SnapshotCohortComponent).where(
                    SnapshotCohortComponent.cohort_id == checkpoint.id
                )
            )
        )
        components_by_kind = {
            component.component_kind: component for component in components
        }
        latest = components_by_kind["latest_reconciliation"]
        assert checkpoint.status == CohortStatus.PARTIAL.value
        assert checkpoint.status_reason == "missed_due_to_capacity"
        assert latest.status == CohortComponentStatus.PARTIAL.value
        assert latest.failure_reason == "baseline_tail_in_progress"
        assert all(
            component.status == CohortComponentStatus.MISSED_DUE_TO_CAPACITY.value
            for kind, component in components_by_kind.items()
            if kind != "latest_reconciliation"
        )
        assert recovery.status == CohortStatus.PLANNED.value

    await engine.dispose()


@pytest.mark.asyncio
async def test_timely_shadow_checkpoint_does_not_create_artificial_recovery() -> None:
    engine, session_factory = await _database()
    checkpoint_at = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    pubdate = checkpoint_at - timedelta(hours=6)
    planner = SnapshotCohortPlanner(_policy())

    async with session_factory() as session:
        await _seed_video(session, bvid="BV-SHADOW-TIMELY", pubdate=pubdate)
        await planner.plan_due(
            session,
            now=checkpoint_at,
            rollout_mode=CohortRolloutMode.SHADOW,
        )
        await planner.plan_due(
            session,
            now=checkpoint_at + timedelta(minutes=60, seconds=1),
            rollout_mode=CohortRolloutMode.SHADOW,
        )

        cohorts = await _cohorts(session, "BV-SHADOW-TIMELY")
        checkpoint = next(row for row in cohorts if row.reason == "age_checkpoint")
        assert checkpoint.extra["shadow_target_status"] == CohortStatus.PLANNED.value
        assert all(row.reason != "recovery" for row in cohorts)

    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_routine_creates_one_gap_and_one_current_archived_probe() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
    pubdate = now - timedelta(days=31)
    policy = _policy()
    planner = SnapshotCohortPlanner(policy)

    async with session_factory() as session:
        await _seed_video(session, bvid="BV-ARCHIVED", pubdate=pubdate)
        await planner.plan_due(
            session,
            now=now - timedelta(days=14),
            rollout_mode=CohortRolloutMode.SHADOW,
        )
        state = await session.get(VideoCollectionState, "BV-ARCHIVED")
        assert state is not None
        state.next_due_at = now - timedelta(days=14)
        session.add_all(
            [
                VideoMetricSnapshot(
                    bvid="BV-ARCHIVED",
                    captured_at=now - timedelta(hours=1),
                    view_count=100,
                ),
                VideoMetricSnapshot(
                    bvid="BV-ARCHIVED",
                    captured_at=now,
                    view_count=100,
                ),
            ]
        )
        await session.flush()

        summary = await planner.plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )

        gaps = list(await session.scalars(select(CollectionScheduleGap)))
        cohorts = [
            cohort
            for cohort in await _cohorts(session, "BV-ARCHIVED")
            if cohort.scheduled_for >= now.replace(second=0, microsecond=0)
        ]
        assert summary.schedule_gaps_created == 1
        assert len(gaps) == 1
        assert gaps[0].reason == "service_offline"
        assert gaps[0].expected_cohort_count == 2
        assert len(cohorts) == 1
        component_kinds = set(
            await session.scalars(
                select(SnapshotCohortComponent.component_kind).where(
                    SnapshotCohortComponent.cohort_id == cohorts[0].id
                )
            )
        )
        assert component_kinds == {"video_metrics"}
        assert state.life_stage == "archived"
        assert (
            state.next_due_at == now + policy.lifecycle.archived_metric_probe_interval
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_candidate_batch_adopts_unseen_videos_before_revisiting_known_rows() -> (
    None
):
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
    planner = SnapshotCohortPlanner(_policy(), batch_limit=1)

    async with session_factory() as session:
        await _seed_video(
            session,
            bvid="BV-OLDEST",
            pubdate=now - timedelta(hours=2),
            first_seen_at=now - timedelta(hours=2),
        )
        await _seed_video(
            session,
            bvid="BV-UNSEEN",
            pubdate=now - timedelta(hours=1),
            first_seen_at=now - timedelta(hours=1),
        )

        await planner.plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )
        assert await session.get(VideoCollectionState, "BV-OLDEST") is not None
        assert await session.get(VideoCollectionState, "BV-UNSEEN") is None

        await planner.plan_due(
            session,
            now=now + timedelta(seconds=30),
            rollout_mode=CohortRolloutMode.SHADOW,
        )

        assert await session.get(VideoCollectionState, "BV-UNSEEN") is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_same_policy_version_rejects_changed_policy_content() -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        await _seed_video(session, bvid="BV-POLICY", pubdate=now - timedelta(hours=1))
        await SnapshotCohortPlanner(_policy()).plan_due(
            session,
            now=now,
            rollout_mode=CohortRolloutMode.SHADOW,
        )
        with pytest.raises(ValueError, match="choose a new policy_version"):
            await SnapshotCohortPlanner(_policy(checkpoint_hours=[3, 9])).plan_due(
                session,
                now=now + timedelta(minutes=1),
                rollout_mode=CohortRolloutMode.SHADOW,
            )

    await engine.dispose()
