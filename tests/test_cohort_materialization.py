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
from books_of_time.domain.enums import TaskKind, TaskStatus


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
            ),
        ),
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
        assert {task.idempotency_key for task in first.tasks} == {
            f"{first.cohort.cohort_key}:{component.component_kind}"
            for component in first.components
        }
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
