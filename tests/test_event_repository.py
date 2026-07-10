from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import (
    CollectionTask,
    EventKeyword,
    EventTarget,
    EventVideo,
)
from books_of_time.db.repositories import EventRepository
from books_of_time.domain.enums import TaskKind, TaskStatus


@pytest.mark.asyncio
async def test_event_repository_resolves_events_by_id_and_slug() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(
            slug=" Ghost-Picture-War ",
            name="鬼图战争",
            game="Example Game",
            description="事件归档测试",
            status="active",
            start_at=now,
            end_at=now + timedelta(days=7),
            timezone="Asia/Shanghai",
            now=now,
        )
        assert (await repository.resolve_event(event.id)).id == event.id
        assert (await repository.resolve_event("ghost-picture-war")).id == event.id
        assert [item.id for item in await repository.list_events()] == [event.id]

    await engine.dispose()


@pytest.mark.asyncio
async def test_add_keyword_target_is_idempotent_and_synchronizes_keyword() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(
            slug="ghost-picture-war",
            name="鬼图战争",
            now=now,
        )
        first = await repository.add_target(
            event_id=event.id,
            target_type="keyword",
            target_value=" 鬼 图   战争 ",
            priority=80,
            now=now,
        )
        second = await repository.add_target(
            event_id=event.id,
            target_type="keyword",
            target_value="鬼 图 战争",
            priority=90,
            now=now + timedelta(minutes=1),
        )
        await session.commit()

        assert first.id == second.id
        assert second.priority == 90
        assert second.last_seen_at == now + timedelta(minutes=1)
        assert await session.scalar(select(func.count(EventTarget.id))) == 1
        keyword = await session.scalar(select(EventKeyword))
        assert keyword is not None
        assert keyword.keyword == "鬼 图 战争"
        assert keyword.normalized_keyword == "鬼 图 战争"
        assert keyword.source_target_id == first.id

    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_bvid_attaches_video_and_enqueues_one_initial_snapshot() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(
            slug="ghost-picture-war",
            name="鬼图战争",
            now=now,
        )
        target = await repository.add_target(
            event_id=event.id,
            target_type="seed_bvid",
            target_value="BV1xx411c7mD",
            priority=100,
            now=now,
        )
        repeated = await repository.add_target(
            event_id=event.id,
            target_type="seed_bvid",
            target_value="BV1xx411c7mD",
            priority=100,
            now=now + timedelta(seconds=1),
        )
        await session.commit()

        assert target.id == repeated.id
        video = await session.get(EventVideo, (event.id, "BV1xx411c7mD"))
        assert video is not None
        assert video.association_reason == "seed_bvid"
        assert video.source_target_id == target.id
        tasks = list(await session.scalars(select(CollectionTask)))
        assert len(tasks) == 1
        assert tasks[0].kind == TaskKind.FETCH_VIDEO_STATS
        assert tasks[0].status == TaskStatus.PENDING
        assert tasks[0].payload["event_id"] == event.id
        assert tasks[0].payload["source_target_id"] == target.id
        assert tasks[0].idempotency_key == (
            f"fetch_video_stats:video:BV1xx411c7mD:event:{event.id}"
        )
        assert [item.bvid for item in await repository.list_videos(event.id)] == [
            "BV1xx411c7mD"
        ]

    await engine.dispose()


@pytest.mark.asyncio
async def test_manual_video_attachment_is_idempotent_and_list_is_bounded() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(
            slug="ghost-picture-war",
            name="鬼图战争",
            now=now,
        )
        first = await repository.attach_video(
            event_id="ghost-picture-war",
            bvid="BV1xx411c7mD",
            association_reason="manual",
            confidence=0.8,
            now=now,
        )
        second = await repository.attach_video(
            event_id=event.id,
            bvid="BV1xx411c7mD",
            association_reason="manual-review",
            confidence=0.95,
            now=now + timedelta(minutes=1),
        )
        await repository.attach_video(
            event_id=event.id,
            bvid="BV1Q541167Qg",
            association_reason="manual",
            now=now,
        )
        await session.commit()

        assert first is second
        assert second.association_reason == "manual-review"
        assert second.confidence == 0.95
        assert second.last_seen_at == now + timedelta(minutes=1)
        assert [
            item.bvid for item in await repository.list_videos(event.id, limit=1)
        ] == ["BV1Q541167Qg"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_event_repository_rejects_missing_event_and_invalid_limit() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        repository = EventRepository(session)
        with pytest.raises(LookupError, match="Event"):
            await repository.resolve_event("missing-event")
        with pytest.raises(ValueError, match="limit"):
            await repository.list_events(limit=0)
        with pytest.raises(ValueError, match="confidence"):
            await repository.attach_video(
                event_id=1,
                bvid="BV1xx411c7mD",
                association_reason="manual",
                confidence=1.1,
                now=datetime(2026, 7, 10, tzinfo=UTC),
            )

    await engine.dispose()
