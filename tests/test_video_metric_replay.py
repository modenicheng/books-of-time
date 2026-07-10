from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.replay import VideoMetricReplayAnalyzer
from books_of_time.db.base import Base
from books_of_time.db.models import VideoMetricSnapshot


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
