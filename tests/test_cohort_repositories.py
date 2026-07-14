from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.cohort_repositories import (
    CollectionPolicyVersionRepository,
    VideoCollectionStateRepository,
)
from books_of_time.db.models import (
    CollectionPolicyVersion,
    CollectionTask,
    KnownVideo,
    SnapshotCohort,
)
from books_of_time.domain.cohort_policy import (
    CollectionTier,
    TierAssessment,
    VideoLifeStage,
)


@pytest.mark.asyncio
async def test_policy_versions_activate_supersede_and_roll_back_immutably() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = CollectionPolicyVersionRepository(session)
        v1 = await repository.create(
            version="cohort-v1",
            policy_kind="snapshot_cohort",
            scope_type="global",
            scope_id=None,
            timezone="Asia/Shanghai",
            policy={"checkpoint_hours": [6, 12, 18, 24]},
            training_window_start=now - timedelta(days=28),
            training_window_end=now,
            distinct_comment_count=2500,
            complete_day_count=20,
            valid_exposure_minutes=10_000,
            excluded_comment_count=50,
            exclusion_reasons={"late_capture": 50},
            algorithm="fixed-defaults-v1",
            created_at=now,
        )
        v2 = await repository.create(
            version="cohort-v2",
            policy_kind="snapshot_cohort",
            scope_type="global",
            scope_id="global",
            timezone="Asia/Shanghai",
            policy={"checkpoint_hours": [6, 12, 18, 24], "revision": 2},
            algorithm="fixed-defaults-v2",
            created_at=now + timedelta(minutes=1),
        )
        await repository.activate(v1.version, activated_at=now + timedelta(minutes=2))
        await repository.activate(v2.version, activated_at=now + timedelta(minutes=3))

        assert v1.active is False
        assert v1.superseded_at == now + timedelta(minutes=3)
        assert v2.active is True
        assert (
            await repository.get_active(
                policy_kind="snapshot_cohort",
                scope_type="global",
                scope_id=None,
            )
            is v2
        )

        await repository.activate(v1.version, activated_at=now + timedelta(minutes=4))

        assert v1.active is True
        assert v1.superseded_at is None
        assert v2.active is False
        assert v2.superseded_at == now + timedelta(minutes=4)
        assert v1.policy == {"checkpoint_hours": [6, 12, 18, 24]}
        assert v1.algorithm == "fixed-defaults-v1"
        assert v1.distinct_comment_count == 2500
        assert v1.complete_day_count == 20
        assert v1.valid_exposure_minutes == 10_000
        assert v1.excluded_comment_count == 50
        assert v1.exclusion_reasons == {"late_capture": 50}
        assert (
            await session.scalar(
                select(func.count()).select_from(CollectionPolicyVersion)
            )
            == 2
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_policy_activation_is_independent_per_game_scope() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = CollectionPolicyVersionRepository(session)
        global_policy = await repository.create(
            version="global-v1",
            policy_kind="snapshot_cohort",
            scope_type="global",
            scope_id=None,
            timezone="Asia/Shanghai",
            policy={},
            algorithm="fixed",
            created_at=now,
        )
        game_policy = await repository.create(
            version="genshin-v1",
            policy_kind="snapshot_cohort",
            scope_type="game",
            scope_id="genshin_impact",
            timezone="Asia/Shanghai",
            policy={},
            algorithm="fixed",
            created_at=now,
        )

        await repository.activate(global_policy.version, activated_at=now)
        await repository.activate(game_policy.version, activated_at=now)

        assert global_policy.active is True
        assert game_policy.active is True
        assert (
            await repository.get_active(
                policy_kind="snapshot_cohort",
                scope_type="game",
                scope_id="genshin_impact",
            )
            is game_policy
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_global_scope_rejects_non_sentinel_id() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        repository = CollectionPolicyVersionRepository(session)
        with pytest.raises(
            ValueError,
            match="global policy scope_id must be empty or 'global'",
        ):
            await repository.create(
                version="invalid-global",
                policy_kind="snapshot_cohort",
                scope_type="global",
                scope_id="genshin_impact",
                timezone="Asia/Shanghai",
                policy={},
                algorithm="fixed",
                created_at=datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
            )

    await engine.dispose()


@pytest.mark.asyncio
async def test_video_adoption_preserves_publish_anchor_and_assessment_boundaries() -> (
    None
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    pubdate = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
    adopted_at = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        policy_repository = CollectionPolicyVersionRepository(session)
        await policy_repository.create(
            version="cohort-v1",
            policy_kind="snapshot_cohort",
            scope_type="global",
            scope_id=None,
            timezone="Asia/Shanghai",
            policy={},
            algorithm="fixed-v1",
            created_at=adopted_at,
        )
        await policy_repository.create(
            version="cohort-v2",
            policy_kind="snapshot_cohort",
            scope_type="global",
            scope_id=None,
            timezone="Asia/Shanghai",
            policy={},
            algorithm="fixed-v2",
            created_at=adopted_at,
        )
        session.add(
            KnownVideo(
                bvid="BV-ADOPT",
                source_mid="42",
                pubdate=pubdate,
                first_seen_at=adopted_at,
                created_at=adopted_at,
                updated_at=adopted_at,
            )
        )
        await session.flush()

        repository = VideoCollectionStateRepository(session)
        state = await repository.adopt(
            bvid="BV-ADOPT",
            policy_version="cohort-v1",
            adopted_at=adopted_at,
        )

        assert state.schedule_anchor_at == pubdate
        assert state.desired_tier == "c"
        assert state.effective_tier == "c"
        assert state.life_stage == "active"
        assert state.next_due_at is None
        assert state.policy_version == "cohort-v1"
        assert state.created_at == adopted_at
        assert state.updated_at == adopted_at

        same_state = await repository.adopt(
            bvid="BV-ADOPT",
            policy_version="cohort-v2",
            adopted_at=adopted_at + timedelta(days=1),
        )

        assert same_state is state
        assert same_state.schedule_anchor_at == pubdate
        assert same_state.policy_version == "cohort-v1"
        assert same_state.updated_at == adopted_at

        state.pinned_tier = "a"
        next_due_at = adopted_at + timedelta(minutes=10)
        updated_at = adopted_at + timedelta(minutes=5)
        assessment = TierAssessment(
            desired=CollectionTier.B,
            effective=CollectionTier.A,
            candidate_downgrade=CollectionTier.B,
            consecutive_downgrade_count=1,
        )
        updated = await repository.apply_assessment(
            bvid="BV-ADOPT",
            assessment=assessment,
            life_stage=VideoLifeStage.DORMANT,
            policy_version="cohort-v2",
            next_due_at=next_due_at,
            updated_at=updated_at,
        )

        assert updated.desired_tier == "b"
        assert updated.effective_tier == "a"
        assert updated.candidate_downgrade_tier == "b"
        assert updated.consecutive_downgrade_count == 1
        assert updated.life_stage == "dormant"
        assert updated.policy_version == "cohort-v2"
        assert updated.next_due_at == next_due_at
        assert updated.updated_at == updated_at
        assert updated.schedule_anchor_at == pubdate
        assert updated.pinned_tier == "a"
        assert (
            await session.scalar(select(func.count()).select_from(CollectionTask)) == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(SnapshotCohort)) == 0
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_repository_methods_flush_without_committing() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = CollectionPolicyVersionRepository(session)
        await repository.create(
            version="rolled-back-v1",
            policy_kind="snapshot_cohort",
            scope_type="global",
            scope_id=None,
            timezone="Asia/Shanghai",
            policy={},
            algorithm="fixed",
            created_at=now,
        )
        await session.rollback()

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(CollectionPolicyVersion).where(
                    CollectionPolicyVersion.version == "rolled-back-v1"
                )
            )
            is None
        )

    await engine.dispose()
