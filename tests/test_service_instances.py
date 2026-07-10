from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.repositories import ServiceInstanceRepository


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_service_instance_repository_tracks_normal_lifecycle() -> None:
    engine, session_factory = await _session_factory()
    started_at = datetime(2026, 7, 10, 1, 0, tzinfo=UTC)
    running_at = started_at + timedelta(seconds=1)
    heartbeat_at = started_at + timedelta(seconds=10)
    stopping_at = started_at + timedelta(seconds=20)
    stopped_at = started_at + timedelta(seconds=21)

    async with session_factory() as session:
        repo = ServiceInstanceRepository(session)
        instance = await repo.register(
            instance_id="service-1",
            hostname="collector-host",
            pid=123,
            version="0.1.0",
            roles=["worker"],
            now=started_at,
        )
        assert instance.status == "starting"
        assert instance.roles == ["worker"]

        await repo.mark_running("service-1", now=running_at)
        await repo.heartbeat("service-1", now=heartbeat_at)
        assert await repo.has_fresh_running_instance(
            now=heartbeat_at + timedelta(seconds=20),
            timeout_seconds=30,
        )

        await repo.mark_stopping("service-1", now=stopping_at)
        await repo.mark_stopped("service-1", now=stopped_at)
        stored = await repo.get("service-1")
        assert stored is not None
        assert stored.status == "stopped"
        assert stored.stopped_at == stopped_at
        assert stored.heartbeat_at == stopped_at
        await session.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_service_instance_repository_rejects_stale_heartbeat() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 1, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = ServiceInstanceRepository(session)
        await repo.register(
            instance_id="service-stale",
            hostname="collector-host",
            pid=124,
            version="0.1.0",
            roles=["worker"],
            now=now - timedelta(minutes=2),
        )
        await repo.mark_running(
            "service-stale",
            now=now - timedelta(minutes=2),
        )

        assert not await repo.has_fresh_running_instance(
            now=now,
            timeout_seconds=30,
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_service_instance_repository_records_bounded_failure() -> None:
    engine, session_factory = await _session_factory()
    now = datetime(2026, 7, 10, 1, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = ServiceInstanceRepository(session)
        await repo.register(
            instance_id="service-failed",
            hostname="collector-host",
            pid=125,
            version="0.1.0",
            roles=["worker"],
            now=now,
        )
        failed = await repo.mark_failed(
            "service-failed",
            now=now + timedelta(seconds=1),
            error_type="x" * 300,
            error_message="message" * 1000,
        )

        assert failed.status == "failed"
        assert failed.stopped_at == now + timedelta(seconds=1)
        assert len(failed.last_error_type or "") == 120
        assert len(failed.last_error_message or "") == 2000

        with pytest.raises(LookupError, match="missing-service"):
            await repo.heartbeat("missing-service", now=now)

    await engine.dispose()
