from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.replay import (
    HotCommentReplayAnalyzer,
    VideoMetricReplayAnalyzer,
)
from books_of_time.db.base import Base
from books_of_time.db.models import (
    CommentObservation,
    CommentObservationMedia,
    RawPageObservation,
    VideoMetricSnapshot,
)
from books_of_time.domain.enums import BilibiliRequestType


@pytest.mark.asyncio
async def test_video_metric_replay_uses_pre_window_baseline_and_raw_evidence() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    async with factory() as session:
        session.add_all(
            [
                _snapshot(start - timedelta(minutes=10), 100, 10, 900),
                _snapshot(start + timedelta(minutes=5), 130, 12, 901),
                _snapshot(start + timedelta(minutes=20), 125, 15, 902),
            ]
        )
        await session.commit()

        rows = await VideoMetricReplayAnalyzer(session).analyze(
            bvid="BV1xx411c7mD",
            since=start,
            until=start + timedelta(hours=1),
        )

    assert len(rows) == 2
    assert rows[0].previous_at == start - timedelta(minutes=10)
    assert rows[0].elapsed_seconds == 900
    assert rows[0].deltas["view_count"] == 30
    assert rows[0].raw_payload_id == 901
    assert rows[0].previous_raw_payload_id == 900
    assert rows[1].deltas["view_count"] == -5
    assert rows[1].deltas["like_count"] == 3
    assert rows[1].as_dict()["schema_version"] == "video-metric-replay-v1"
    await engine.dispose()


@pytest.mark.asyncio
async def test_hot_comment_replay_restores_ordered_text_authors_and_media() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    bvid = "BV1xx411c7mD"
    async with factory() as session:
        session.add_all(
            [
                _hot_page(501, bvid, start, "success"),
                _hot_page(502, bvid, start + timedelta(minutes=10), "failed"),
                _hot_comment(101, 1001, bvid, start, 501, 1),
                _hot_comment(102, 1002, bvid, start, 501, 2),
                _hot_comment(103, 1003, bvid, start, 501, 3),
                CommentObservationMedia(
                    id=1,
                    comment_observation_id=101,
                    bvid=bvid,
                    rpid=1001,
                    media_source_id=71,
                    media_asset_id=81,
                    position=0,
                    role="comment_image",
                    raw_page_id=501,
                    created_at=start,
                ),
            ]
        )
        await session.commit()

        rows = await HotCommentReplayAnalyzer(session).analyze(
            bvid=bvid,
            since=start,
            until=start + timedelta(hours=1),
            top_n=2,
        )

    assert len(rows) == 1
    snapshot = rows[0]
    assert snapshot.raw_page_observation_id == 501
    assert snapshot.raw_payload_id == 501
    assert [comment["rpid"] for comment in snapshot.comments] == [1001, 1002]
    assert snapshot.comments[0]["content"] == "comment-1001"
    assert snapshot.comments[0]["author_name"] == "user-1001"
    assert snapshot.comments[0]["comment_observation_id"] == 101
    assert snapshot.comments[0]["media"] == [
        {
            "position": 0,
            "role": "comment_image",
            "media_source_id": 71,
            "media_asset_id": 81,
        }
    ]
    assert snapshot.as_dict()["schema_version"] == "hot-comment-replay-v1"
    await engine.dispose()


def _snapshot(
    captured_at: datetime,
    view_count: int,
    like_count: int,
    raw_payload_id: int,
) -> VideoMetricSnapshot:
    return VideoMetricSnapshot(
        bvid="BV1xx411c7mD",
        captured_at=captured_at,
        view_count=view_count,
        like_count=like_count,
        coin_count=2,
        favorite_count=3,
        share_count=4,
        reply_count=5,
        danmaku_count=6,
        raw_payload_id=raw_payload_id,
    )


def _hot_page(
    page_id: int,
    bvid: str,
    captured_at: datetime,
    status: str,
) -> RawPageObservation:
    return RawPageObservation(
        id=page_id,
        raw_payload_id=page_id,
        captured_at=captured_at,
        request_type=BilibiliRequestType.COMMENT_HOT,
        target_type="video",
        target_id=bvid,
        page_number=1,
        cursor=None,
        sort_mode="hot",
        parser_version="test",
        status=status,
        item_count=3,
        extra={},
    )


def _hot_comment(
    observation_id: int,
    rpid: int,
    bvid: str,
    captured_at: datetime,
    raw_page_id: int,
    position: int,
) -> CommentObservation:
    return CommentObservation(
        id=observation_id,
        rpid=rpid,
        bvid=bvid,
        oid=777,
        captured_at=captured_at,
        raw_payload_id=raw_page_id,
        raw_page_observation_id=raw_page_id,
        sort_mode="hot",
        page_number=1,
        position=position,
        content=f"comment-{rpid}",
        content_hash=bytes([position]) * 32,
        like_count=position,
        reply_count=0,
        author_mid=rpid,
        author_name=f"user-{rpid}",
        is_deleted=False,
        visibility="visible",
        extra={},
    )
