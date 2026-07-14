from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import (
    CollectionPolicyVersion,
    CollectionScheduleGap,
    CollectionTask,
    KnownVideo,
    KnownVideoSource,
    SnapshotCohort,
    SnapshotCohortComponent,
    VideoCollectionState,
    VideoMetricSnapshot,
)
from books_of_time.domain.cohort_policy import (
    CohortPolicy,
    CohortRolloutMode,
    CohortStatus,
)
from books_of_time.task_orchestrator.snapshot_cohort_planner import (
    SnapshotCohortPlanner,
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
        assert checkpoint.status == CohortStatus.MISSED.value
        assert checkpoint.status_reason == "missed_due_to_capacity"
        assert all(
            component.status == "missed_due_to_capacity" for component in components
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
