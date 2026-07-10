from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.repositories import ScheduledJobRepository
from books_of_time.domain.enums import ScheduledJobKind


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_scheduled_job_ensure_is_idempotent_and_preserves_schedule() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
    async with session_factory() as session:
        repo = ScheduledJobRepository(session)
        first = await repo.ensure(
            job_key="uid-discovery",
            job_kind=ScheduledJobKind.UID_DISCOVERY,
            schedule_seconds=60,
            priority=100,
            payload={"source": "initial"},
            next_run_at=now,
        )
        first_id = first.id
        await session.commit()

    async with session_factory() as session:
        repo = ScheduledJobRepository(session)
        second = await repo.ensure(
            job_key="uid-discovery",
            job_kind=ScheduledJobKind.UID_DISCOVERY,
            schedule_seconds=120,
            priority=90,
            payload={"source": "updated"},
            next_run_at=now + timedelta(hours=1),
        )
        await session.commit()

        assert second.id == first_id
        assert second.next_run_at == now
        assert second.schedule_seconds == 120
        assert second.priority == 90
        assert second.payload == {"source": "updated"}

    await engine.dispose()


@pytest.mark.asyncio
async def test_scheduled_job_lease_prefers_priority_and_is_exclusive() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
    async with session_factory() as session:
        repo = ScheduledJobRepository(session)
        for key, priority in (("low", 10), ("high", 100)):
            await repo.ensure(
                job_key=key,
                job_kind=ScheduledJobKind.VIDEO_SNAPSHOT_SWEEP,
                schedule_seconds=60,
                priority=priority,
                payload={},
                next_run_at=now,
            )
        await session.commit()

    async with session_factory() as session:
        repo = ScheduledJobRepository(session)
        first = await repo.lease_due(
            lease_owner="coordinator-1",
            now=now,
            lease_seconds=30,
        )
        second = await repo.lease_due(
            lease_owner="coordinator-1",
            now=now,
            lease_seconds=30,
        )
        await session.commit()

        assert first is not None
        assert first.job_key == "high"
        assert first.lease_owner == "coordinator-1"
        assert first.lease_until == now + timedelta(seconds=30)
        assert second is not None
        assert second.job_key == "low"

    await engine.dispose()


@pytest.mark.asyncio
async def test_scheduled_job_expired_lease_can_be_recovered() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
    async with session_factory() as session:
        repo = ScheduledJobRepository(session)
        await repo.ensure(
            job_key="terminal",
            job_kind=ScheduledJobKind.DAILY_TERMINAL_SNAPSHOT,
            schedule_seconds=60,
            priority=80,
            payload={},
            next_run_at=now,
        )
        await session.commit()

    async with session_factory() as session:
        repo = ScheduledJobRepository(session)
        leased = await repo.lease_due(
            lease_owner="dead-coordinator",
            now=now,
            lease_seconds=5,
        )
        await session.commit()
        assert leased is not None

    async with session_factory() as session:
        recovered = await ScheduledJobRepository(session).lease_due(
            lease_owner="new-coordinator",
            now=now + timedelta(seconds=6),
            lease_seconds=5,
        )
        assert recovered is not None
        assert recovered.job_key == "terminal"
        assert recovered.lease_owner == "new-coordinator"

    await engine.dispose()


@pytest.mark.asyncio
async def test_scheduled_job_success_advances_to_first_future_aligned_slot() -> None:
    engine, session_factory = await _session_factory()
    scheduled_at = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
    finished_at = scheduled_at + timedelta(minutes=3, seconds=30)
    async with session_factory() as session:
        repo = ScheduledJobRepository(session)
        await repo.ensure(
            job_key="sweep",
            job_kind=ScheduledJobKind.VIDEO_SNAPSHOT_SWEEP,
            schedule_seconds=60,
            priority=80,
            payload={},
            next_run_at=scheduled_at,
        )
        job = await repo.lease_due(
            lease_owner="coordinator",
            now=scheduled_at,
            lease_seconds=30,
        )
        assert job is not None

        await repo.mark_succeeded(job, now=finished_at)
        await session.commit()

        assert job.next_run_at == scheduled_at + timedelta(minutes=4)
        assert job.last_succeeded_at == finished_at
        assert job.consecutive_failures == 0
        assert job.lease_owner is None
        assert job.lease_until is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_scheduled_job_failure_records_retry_and_bounded_diagnostics() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
    async with session_factory() as session:
        repo = ScheduledJobRepository(session)
        await repo.ensure(
            job_key="failure",
            job_kind=ScheduledJobKind.UID_DISCOVERY,
            schedule_seconds=60,
            priority=100,
            payload={},
            next_run_at=now,
        )
        job = await repo.lease_due(
            lease_owner="coordinator",
            now=now,
            lease_seconds=30,
        )
        assert job is not None

        await repo.mark_failed(
            job,
            now=now + timedelta(seconds=1),
            retry_delay_seconds=15,
            error=RuntimeError("broken" * 1000),
        )
        await session.commit()

        assert job.consecutive_failures == 1
        assert job.next_run_at == now + timedelta(seconds=16)
        assert job.last_error_type == "RuntimeError"
        assert len(job.last_error_message or "") == 2000
        assert job.lease_owner is None
        assert job.lease_until is None

    await engine.dispose()
