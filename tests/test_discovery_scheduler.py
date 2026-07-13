from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.models import (
    Base,
    CollectionTask,
    EventVideo,
    KnownVideo,
    KnownVideoSource,
    RawPageObservation,
)
from books_of_time.db.repositories import EventRepository
from books_of_time.domain.enums import BilibiliRequestType, TaskKind
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
async def test_discovery_scheduler_preserves_all_source_provenance() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    first_seen = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    second_seen = first_seen + timedelta(minutes=1)
    third_seen = second_seen + timedelta(minutes=1)
    game_source = {
        "source_mid": "123",
        "pool_type": "game",
        "pool_id": "genshin",
        "game_id": "genshin",
        "official": True,
        "monitored": True,
    }
    event_source = {
        "source_mid": "123",
        "pool_type": "event",
        "pool_id": "target:9",
        "game_id": None,
        "official": False,
        "monitored": True,
    }
    changed_game_source = {
        **game_source,
        "game_id": "conflicting-game",
        "official": False,
        "monitored": False,
    }
    scheduler = DiscoveryScheduler(session_factory=session_factory)

    async with session_factory() as session:
        first_page = RawPageObservation(
            raw_payload_id=1,
            captured_at=first_seen,
            request_type=BilibiliRequestType.USER_VIDEO_LIST,
            target_type="user",
            target_id="123",
            page_number=1,
            cursor=None,
            sort_mode="pubdate",
            parser_version="test",
            status="success",
            item_count=1,
            extra={},
        )
        second_page = RawPageObservation(
            raw_payload_id=2,
            captured_at=second_seen,
            request_type=BilibiliRequestType.USER_VIDEO_LIST,
            target_type="user",
            target_id="123",
            page_number=1,
            cursor=None,
            sort_mode="pubdate",
            parser_version="test",
            status="success",
            item_count=1,
            extra={},
        )
        session.add_all([first_page, second_page])
        await session.flush()

        video = DiscoveredVideo(
            bvid="BV-SOURCES",
            pubdate=first_seen - timedelta(seconds=30),
            source_mid="123",
        )
        first_created = await scheduler.handle_discovered_videos(
            session=session,
            videos=[video],
            source_associations=[game_source],
            raw_page_observation_id=first_page.id,
            now=first_seen,
        )
        second_created = await scheduler.handle_discovered_videos(
            session=session,
            videos=[video],
            source_associations=[changed_game_source, event_source],
            raw_page_observation_id=second_page.id,
            now=second_seen,
        )
        third_created = await scheduler.handle_discovered_videos(
            session=session,
            videos=[video],
            source_associations=[game_source, event_source],
            now=third_seen,
        )
        await session.commit()

    async with session_factory() as session:
        known_video = await session.get(KnownVideo, "BV-SOURCES")
        sources = list(
            await session.scalars(
                select(KnownVideoSource).order_by(KnownVideoSource.pool_type)
            )
        )
        tasks = list(await session.scalars(select(CollectionTask)))

    assert first_created == ["BV-SOURCES"]
    assert second_created == []
    assert third_created == []
    assert known_video is not None
    assert known_video.source_mid == "123"
    assert len(tasks) == 1
    assert [source.pool_type for source in sources] == ["event", "game"]
    event_row, game_row = sources
    assert event_row.first_raw_page_id == second_page.id
    assert event_row.last_raw_page_id == second_page.id
    assert event_row.first_seen_at == second_seen
    assert event_row.last_seen_at == third_seen
    assert game_row.first_raw_page_id == first_page.id
    assert game_row.last_raw_page_id == second_page.id
    assert game_row.first_seen_at == first_seen
    assert game_row.last_seen_at == third_seen
    assert game_row.game_id == "genshin"
    assert game_row.official is True
    assert game_row.monitored is True
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
