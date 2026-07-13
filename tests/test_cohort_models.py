from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import (
    CollectionCoverageStat,
    CollectionPolicyVersion,
    CollectionScheduleGap,
    CollectionTask,
    KnownVideo,
    SnapshotCohort,
    SnapshotCohortComponent,
    VideoCollectionState,
)


def test_cohort_model_metadata_contracts() -> None:
    assert CollectionPolicyVersion.__table__.c.version.unique is True
    assert VideoCollectionState.__table__.c.bvid.primary_key is True
    assert SnapshotCohort.__table__.c.cohort_key.unique is True
    assert SnapshotCohortComponent.__table__.c.cohort_id.nullable is False
    assert CollectionScheduleGap.__table__.c.expected_cohort_count.nullable is False
    assert CollectionTask.__table__.c.snapshot_cohort_id.nullable is True
    assert (
        CollectionCoverageStat.__table__.c.snapshot_cohort_component_id.nullable is True
    )


@pytest.mark.asyncio
async def test_cohort_models_round_trip_defaults_utc_and_json() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 2, 0, tzinfo=UTC)

    async with session_factory() as session:
        policy = CollectionPolicyVersion(
            version="cohort-default-v1",
            policy_kind="snapshot_cohort",
            scope_type="global",
            scope_id="global",
            timezone="Asia/Shanghai",
            policy={"checkpoint_hours": [6, 12, 18, 24]},
            exclusion_reasons={"missing_pubdate": 2},
            algorithm="fixed-defaults",
            created_at=now,
        )
        video = KnownVideo(
            bvid="BV-COHORT",
            source_mid="42",
            pubdate=now,
            first_seen_at=now + timedelta(minutes=5),
        )
        session.add_all([policy, video])
        await session.flush()

        state = VideoCollectionState(
            bvid=video.bvid,
            schedule_anchor_at=video.pubdate,
            policy_version=policy.version,
            extra={"adopted_by": "test"},
            created_at=now,
            updated_at=now,
        )
        cohort = SnapshotCohort(
            cohort_key="snapshot:BV-COHORT:age:6h",
            bvid=video.bvid,
            scheduled_for=now + timedelta(hours=6),
            reason="checkpoint",
            age_checkpoint_hours=6,
            desired_tier="s",
            effective_tier="s",
            policy_version=policy.version,
            deadline=now + timedelta(hours=7),
            expected_component_count=1,
            extra={"source": "checkpoint"},
            created_at=now,
            updated_at=now,
        )
        gap = CollectionScheduleGap(
            bvid=video.bvid,
            gap_start=now + timedelta(hours=1),
            gap_end=now + timedelta(hours=2),
            expected_cohort_count=2,
            reason="service_offline",
            service_instance_id="service-test",
            policy_version=policy.version,
            created_at=now,
        )
        session.add_all([state, cohort, gap])
        await session.flush()

        component = SnapshotCohortComponent(
            cohort_id=cohort.id,
            component_kind="video_metrics",
            scheduled_for=cohort.scheduled_for,
            deadline=cohort.deadline,
            extra={"required_fields": ["view", "reply"]},
        )
        session.add(component)
        await session.commit()

    async with session_factory() as session:
        stored_policy = await session.scalar(select(CollectionPolicyVersion))
        stored_state = await session.get(VideoCollectionState, "BV-COHORT")
        stored_cohort = await session.scalar(select(SnapshotCohort))
        stored_component = await session.scalar(select(SnapshotCohortComponent))
        stored_gap = await session.scalar(select(CollectionScheduleGap))

        assert stored_policy is not None
        assert stored_state is not None
        assert stored_cohort is not None
        assert stored_component is not None
        assert stored_gap is not None
        assert stored_policy.policy == {"checkpoint_hours": [6, 12, 18, 24]}
        assert stored_policy.exclusion_reasons == {"missing_pubdate": 2}
        assert stored_policy.distinct_comment_count == 0
        assert stored_policy.active is False
        assert stored_state.desired_tier == "c"
        assert stored_state.effective_tier == "c"
        assert stored_state.life_stage == "active"
        assert stored_state.schedule_anchor_at == now
        assert stored_state.schedule_anchor_at.tzinfo is UTC
        assert stored_state.extra == {"adopted_by": "test"}
        assert stored_cohort.status == "planned"
        assert stored_cohort.completed_component_count == 0
        assert stored_cohort.extra == {"source": "checkpoint"}
        assert stored_component.required is True
        assert stored_component.status == "pending"
        assert stored_component.planned_pages == 0
        assert stored_component.extra == {"required_fields": ["view", "reply"]}
        assert stored_gap.expected_cohort_count == 2

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_kind", ["cohort_key", "component_kind"])
async def test_cohort_identity_constraints_are_unique(duplicate_kind: str) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 2, 0, tzinfo=UTC)

    async with session_factory() as session:
        session.add_all(
            [
                CollectionPolicyVersion(
                    version="cohort-default-v1",
                    policy_kind="snapshot_cohort",
                    scope_type="global",
                    scope_id="global",
                    timezone="Asia/Shanghai",
                    policy={},
                    exclusion_reasons={},
                    algorithm="fixed-defaults",
                    created_at=now,
                ),
                KnownVideo(
                    bvid="BV-UNIQUE-COHORT",
                    pubdate=now,
                    first_seen_at=now,
                ),
            ]
        )
        await session.flush()
        first = SnapshotCohort(
            cohort_key="snapshot:BV-UNIQUE-COHORT:age:6h",
            bvid="BV-UNIQUE-COHORT",
            scheduled_for=now + timedelta(hours=6),
            reason="checkpoint",
            age_checkpoint_hours=6,
            desired_tier="s",
            effective_tier="s",
            policy_version="cohort-default-v1",
            expected_component_count=1,
            created_at=now,
            updated_at=now,
        )
        session.add(first)
        await session.flush()

        if duplicate_kind == "cohort_key":
            session.add(
                SnapshotCohort(
                    cohort_key=first.cohort_key,
                    bvid=first.bvid,
                    scheduled_for=now + timedelta(hours=7),
                    reason="routine",
                    desired_tier="s",
                    effective_tier="s",
                    policy_version="cohort-default-v1",
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            session.add_all(
                [
                    SnapshotCohortComponent(
                        cohort_id=first.id,
                        component_kind="video_metrics",
                        scheduled_for=first.scheduled_for,
                    ),
                    SnapshotCohortComponent(
                        cohort_id=first.id,
                        component_kind="video_metrics",
                        scheduled_for=first.scheduled_for,
                    ),
                ]
            )

        with pytest.raises(IntegrityError):
            await session.commit()

    await engine.dispose()
