from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import VideoMetricSnapshot
from books_of_time.db.repositories import VideoMetricSnapshotRepository


@pytest.mark.asyncio
async def test_video_metric_repository_lists_snapshots_newest_first() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    older_time = datetime(2099, 1, 1, 0, 0, tzinfo=UTC)
    newer_time = datetime(2099, 1, 1, 0, 1, tzinfo=UTC)
    async with session_factory() as session:
        session.add_all(
            [
                VideoMetricSnapshot(
                    bvid="BV1abc",
                    captured_at=older_time,
                    view_count=100,
                    like_count=10,
                    coin_count=1,
                    favorite_count=2,
                    share_count=3,
                    reply_count=4,
                    danmaku_count=5,
                    raw_payload_id=1,
                ),
                VideoMetricSnapshot(
                    bvid="BV1abc",
                    captured_at=newer_time,
                    view_count=200,
                    like_count=20,
                    coin_count=2,
                    favorite_count=3,
                    share_count=4,
                    reply_count=5,
                    danmaku_count=6,
                    raw_payload_id=2,
                ),
                VideoMetricSnapshot(
                    bvid="BVOTHER",
                    captured_at=newer_time,
                    view_count=999,
                    like_count=999,
                    coin_count=999,
                    favorite_count=999,
                    share_count=999,
                    reply_count=999,
                    danmaku_count=999,
                    raw_payload_id=3,
                ),
            ]
        )
        await session.commit()

        rows = await VideoMetricSnapshotRepository(session).list_for_bvid(
            bvid="BV1abc",
            limit=1,
        )

    assert [row.captured_at for row in rows] == [newer_time]
    assert [row.view_count for row in rows] == [200]

    await engine.dispose()
