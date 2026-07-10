from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.models import Base, CollectionTask, EventVideo, KnownVideo
from books_of_time.db.repositories import EventRepository
from books_of_time.domain.enums import TaskKind
from books_of_time.task_orchestrator.discovery import (
    DiscoveredVideo,
    DiscoveryScheduler,
    EventDiscoveryLink,
)


@pytest.mark.asyncio
async def test_discovery_scheduler_records_new_video_and_enqueues_stats_task() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    scheduler = DiscoveryScheduler(session_factory=session_factory)

    async with session_factory() as session:
        created = await scheduler.handle_discovered_videos(
            session=session,
            videos=[
                DiscoveredVideo(
                    bvid="BVNEW",
                    pubdate=now - timedelta(seconds=60),
                    source_mid="123",
                ),
                DiscoveredVideo(
                    bvid="BVOLD",
                    pubdate=now - timedelta(minutes=10),
                    source_mid="123",
                ),
            ],
            now=now,
        )
        await session.commit()

    assert created == ["BVNEW", "BVOLD"]

    async with session_factory() as session:
        known_videos = (await session.scalars(select(KnownVideo))).all()
        tasks = (await session.scalars(select(CollectionTask))).all()

        assert [video.bvid for video in known_videos] == ["BVNEW", "BVOLD"]
        assert len(tasks) == 2
        assert [task.kind for task in tasks] == [
            TaskKind.FETCH_VIDEO_STATS,
            TaskKind.FETCH_VIDEO_STATS,
        ]
        assert [task.target_id for task in tasks] == ["BVNEW", "BVOLD"]
        assert [task.not_before for task in tasks] == [now, now]
        assert tasks[0].payload["reason"] == "fresh_discovery"
        assert tasks[0].priority == 100
        assert tasks[1].payload["reason"] == "delayed_discovery"
        assert tasks[1].priority == 90

    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_scheduler_associates_event_for_new_and_known_video() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(
            slug="event-a",
            name="事件 A",
            now=now,
        )
        target = await repository.add_target(
            event_id=event.id,
            target_type="uid",
            target_value="123",
            now=now,
        )
        session.add(
            KnownVideo(
                bvid="BV1Q541167Qg",
                source_mid="123",
                pubdate=now - timedelta(minutes=2),
                first_seen_at=now - timedelta(minutes=1),
            )
        )
        await session.commit()

    scheduler = DiscoveryScheduler(session_factory=session_factory)
    links = [EventDiscoveryLink(event_id=event.id, target_id=target.id)]
    async with session_factory() as session:
        created = await scheduler.handle_discovered_videos(
            session=session,
            videos=[
                DiscoveredVideo(
                    bvid="BV1xx411c7mD",
                    pubdate=now,
                    source_mid="123",
                ),
                DiscoveredVideo(
                    bvid="BV1Q541167Qg",
                    pubdate=now - timedelta(minutes=2),
                    source_mid="123",
                ),
            ],
            event_links=links,
            now=now,
        )
        await session.commit()

    async with session_factory() as session:
        event_videos = list(
            await session.scalars(select(EventVideo).order_by(EventVideo.bvid.asc()))
        )
        tasks = list(await session.scalars(select(CollectionTask)))

    assert created == ["BV1xx411c7mD"]
    assert [video.bvid for video in event_videos] == [
        "BV1Q541167Qg",
        "BV1xx411c7mD",
    ]
    assert all(video.source_target_id == target.id for video in event_videos)
    assert all(video.association_reason == "uid_target" for video in event_videos)
    assert len(tasks) == 1
    assert tasks[0].payload["event_links"] == [
        {"event_id": event.id, "target_id": target.id}
    ]
    await engine.dispose()
