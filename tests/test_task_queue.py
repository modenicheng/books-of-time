from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.models import Base
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind, TaskStatus


@pytest.mark.asyncio
async def test_task_repository_leases_highest_priority_due_task() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = CollectionTaskRepository(session)
        await repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BVLOW",
            priority=10,
            payload={"bvid": "BVLOW"},
            not_before=now - timedelta(seconds=1),
        )
        await repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BVHIGH",
            priority=99,
            payload={"bvid": "BVHIGH"},
            not_before=now - timedelta(seconds=1),
        )
        await session.commit()

        task = await repo.lease_next(
            lease_owner="worker-1",
            now=now,
            lease_seconds=120,
        )

        assert task is not None
        assert task.target_id == "BVHIGH"
        assert task.status == TaskStatus.RUNNING
        assert task.lease_owner == "worker-1"
        assert task.lease_until == now + timedelta(seconds=120)

    await engine.dispose()
