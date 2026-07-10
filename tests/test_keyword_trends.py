from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.keywords import KeywordTrendAnalyzer
from books_of_time.db.base import Base
from books_of_time.db.models import CommentObservation, EventVideo
from books_of_time.db.repositories import EventRepository


@pytest.mark.asyncio
async def test_keyword_trends_count_distinct_comments_and_observations() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(
            slug="event-a",
            name="事件 A",
            now=start,
        )
        await repository.add_target(
            event_id=event.id,
            target_type="keyword",
            target_value="控评",
            now=start,
        )
        for bvid in ("BV1xx411c7mD", "BV1Q541167Qg", "BV17x411w7KC"):
            await repository.attach_video(
                event_id=event.id,
                bvid=bvid,
                association_reason="manual",
                now=start,
            )
        inactive_video = await session.get(EventVideo, (event.id, "BV17x411w7KC"))
        assert inactive_video is not None
        inactive_video.active = False
        session.add_all(
            [
                _observation(1, "BV1xx411c7mD", 1001, start, "质疑控评"),
                _observation(
                    2,
                    "BV1xx411c7mD",
                    1001,
                    start + timedelta(minutes=10),
                    "仍在质疑控评",
                ),
                _observation(3, "BV1xx411c7mD", 1002, start, "普通评论"),
                _observation(4, "BV1Q541167Qg", 1003, start, "控评了吗"),
                _observation(5, "BV17x411w7KC", 1004, start, "失活视频也提到控评"),
            ]
        )
        await session.commit()

        analyzer = KeywordTrendAnalyzer(session)
        event_points = await analyzer.analyze(
            event_reference=event.id,
            since=start,
            until=start + timedelta(hours=2),
            bucket_seconds=3600,
        )
        video_points = await analyzer.analyze(
            event_reference="event-a",
            since=start,
            until=start + timedelta(hours=2),
            bucket_seconds=3600,
            bvid="BV1xx411c7mD",
        )

    assert len(event_points) == 2
    assert event_points[0].scope_type == "event"
    assert event_points[0].scope_id == "event-a"
    assert event_points[0].keyword == "控评"
    assert event_points[0].distinct_comment_count == 2
    assert event_points[0].observation_count == 3
    assert event_points[1].distinct_comment_count == 0
    assert event_points[1].observation_count == 0
    assert video_points[0].scope_type == "video"
    assert video_points[0].scope_id == "BV1xx411c7mD"
    assert video_points[0].distinct_comment_count == 1
    assert video_points[0].observation_count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_keyword_trends_reject_invalid_window_and_unassociated_video() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        await repository.create_event(slug="event-a", name="事件 A", now=now)
        analyzer = KeywordTrendAnalyzer(session)
        with pytest.raises(ValueError, match="until"):
            await analyzer.analyze(
                event_reference="event-a",
                since=now,
                until=now,
                bucket_seconds=3600,
            )
        with pytest.raises(ValueError, match="associated"):
            await analyzer.analyze(
                event_reference="event-a",
                since=now,
                until=now + timedelta(hours=1),
                bucket_seconds=3600,
                bvid="BV1xx411c7mD",
            )

    await engine.dispose()


def _observation(
    observation_id: int,
    bvid: str,
    rpid: int,
    captured_at: datetime,
    content: str,
) -> CommentObservation:
    return CommentObservation(
        id=observation_id,
        rpid=rpid,
        bvid=bvid,
        oid=777,
        captured_at=captured_at,
        raw_payload_id=observation_id,
        raw_page_observation_id=observation_id,
        sort_mode="hot",
        page_number=1,
        position=1,
        content=content,
        content_hash=bytes([observation_id]) * 32,
        like_count=1,
        reply_count=0,
        author_mid=42,
        author_name="user",
        is_deleted=False,
        visibility="visible",
        extra={},
    )
