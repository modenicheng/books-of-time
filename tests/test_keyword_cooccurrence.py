from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.keywords import KeywordCooccurrenceAnalyzer
from books_of_time.db.base import Base
from books_of_time.db.models import CommentObservation, EventVideo
from books_of_time.db.repositories import EventRepository


@pytest.mark.asyncio
async def test_keyword_cooccurrence_counts_distinct_comments_and_observations() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(slug="event-a", name="事件 A", now=start)
        for keyword in ("控评", "删评", "辟谣"):
            await repository.add_target(
                event_id=event.id,
                target_type="keyword",
                target_value=keyword,
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
                _observation(1, "BV1xx411c7mD", 1001, start, "控评删评"),
                _observation(2, "BV1xx411c7mD", 1001, start, "仍在控评删评"),
                _observation(3, "BV1Q541167Qg", 1002, start, "控评删评辟谣"),
                _observation(4, "BV1Q541167Qg", 1003, start, "只有控评"),
                _observation(5, "BV17x411w7KC", 1004, start, "控评删评辟谣"),
            ]
        )
        await session.commit()

        analyzer = KeywordCooccurrenceAnalyzer(session)
        edges = await analyzer.analyze(
            event_reference="event-a",
            since=start,
            until=start + timedelta(hours=1),
        )
        video_edges = await analyzer.analyze(
            event_reference=event.id,
            since=start,
            until=start + timedelta(hours=1),
            bvid="BV1xx411c7mD",
        )

    by_pair = {(edge.keyword_a, edge.keyword_b): edge for edge in edges}
    assert set(by_pair) == {("删评", "控评"), ("删评", "辟谣"), ("控评", "辟谣")}
    primary = by_pair[("删评", "控评")]
    assert primary.distinct_comment_count == 2
    assert primary.observation_count == 3
    assert primary.scope_type == "event"
    assert video_edges[0].scope_type == "video"
    assert video_edges[0].scope_id == "BV1xx411c7mD"
    assert video_edges[0].distinct_comment_count == 1
    assert video_edges[0].observation_count == 2
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
