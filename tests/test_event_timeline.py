from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import (
    CommentStateEvent,
    CommentVisibilityEvent,
    VideoMetricSnapshot,
)
from books_of_time.db.repositories import EventRepository


@pytest.mark.asyncio
async def test_event_timeline_is_ordered_and_preserves_evidence_references() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    bvid = "BV1xx411c7mD"

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(
            slug="event-a",
            name="事件 A",
            now=now,
        )
        await repository.attach_video(
            event_id=event.id,
            bvid=bvid,
            association_reason="manual",
            confidence=0.9,
            now=now,
        )
        session.add_all(
            [
                VideoMetricSnapshot(
                    bvid=bvid,
                    captured_at=now + timedelta(minutes=1),
                    view_count=100,
                    like_count=10,
                    coin_count=3,
                    favorite_count=4,
                    share_count=5,
                    reply_count=6,
                    danmaku_count=7,
                    raw_payload_id=101,
                ),
                CommentStateEvent(
                    rpid=2001,
                    bvid=bvid,
                    previous_comment_observation_id=301,
                    current_comment_observation_id=302,
                    event_type="LIKE_BUCKET_CHANGED",
                    old_value={"like_bucket": "10-99"},
                    new_value={"like_bucket": "100-999"},
                    created_at=now + timedelta(minutes=2),
                ),
                CommentVisibilityEvent(
                    rpid=2001,
                    bvid=bvid,
                    previous_comment_observation_id=302,
                    current_comment_observation_id=None,
                    event_type="DISAPPEARED",
                    old_visibility="visible",
                    new_visibility="missing",
                    missing_reason="missing_after_seen",
                    created_at=now + timedelta(minutes=3),
                ),
                VideoMetricSnapshot(
                    bvid="BV1Q541167Qg",
                    captured_at=now,
                    view_count=999,
                    raw_payload_id=999,
                ),
            ]
        )
        await session.commit()

        rows = await repository.build_timeline("event-a")

    assert [row.record_type for row in rows] == [
        "event_video_associated",
        "video_metric_snapshot",
        "comment_state_event",
        "comment_visibility_event",
    ]
    assert [row.timestamp for row in rows] == sorted(row.timestamp for row in rows)
    assert all(row.event_id == event.id for row in rows)
    assert all(row.event_slug == "event-a" for row in rows)
    assert all(row.bvid == bvid for row in rows)
    assert rows[0].data == {
        "active": True,
        "association_reason": "manual",
        "confidence": 0.9,
        "source_target_id": None,
    }
    assert rows[1].data["raw_payload_id"] == 101
    assert rows[1].data["view_count"] == 100
    assert rows[2].source_key.startswith("comment_state_events:")
    assert rows[2].data["current_comment_observation_id"] == 302
    assert rows[3].data["missing_reason"] == "missing_after_seen"
    assert rows[3].as_dict()["timestamp"] == "2026-07-10T10:03:00+00:00"
    await engine.dispose()


@pytest.mark.asyncio
async def test_event_timeline_empty_event_returns_no_rows() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        repository = EventRepository(session)
        await repository.create_event(
            slug="empty-event",
            name="空事件",
            now=datetime(2026, 7, 10, tzinfo=UTC),
        )
        assert await repository.build_timeline("empty-event") == []

    await engine.dispose()
