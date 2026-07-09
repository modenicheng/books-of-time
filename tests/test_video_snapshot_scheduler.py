from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import CollectionTask, KnownVideo, VideoMetricSnapshot
from books_of_time.domain.enums import TaskKind
from books_of_time.task_orchestrator.video_snapshot_scheduler import (
    VideoSnapshotScheduler,
)


def _metric(
    *,
    bvid: str,
    captured_at: datetime,
    view_count: int,
) -> VideoMetricSnapshot:
    return VideoMetricSnapshot(
        bvid=bvid,
        captured_at=captured_at,
        view_count=view_count,
        like_count=None,
        coin_count=None,
        favorite_count=None,
        share_count=None,
        reply_count=None,
        danmaku_count=None,
        raw_payload_id=None,
    )


@pytest.mark.asyncio
async def test_video_snapshot_scheduler_enqueues_next_stats_task_for_known_video() -> (
    None
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    published_at = datetime(2026, 7, 8, 3, 0, tzinfo=UTC)
    now = datetime(2026, 7, 8, 10, 7, tzinfo=UTC)
    async with session_factory() as session:
        session.add(
            KnownVideo(
                bvid="BV1abc",
                source_mid="123",
                pubdate=published_at,
                first_seen_at=now - timedelta(hours=7),
            )
        )
        session.add_all(
            [
                _metric(
                    bvid="BV1abc",
                    captured_at=now - timedelta(minutes=70),
                    view_count=1000,
                ),
                _metric(
                    bvid="BV1abc",
                    captured_at=now - timedelta(minutes=5),
                    view_count=33_000,
                ),
            ]
        )
        await session.commit()

        task = await VideoSnapshotScheduler().schedule_next_for_video(
            session=session,
            bvid="BV1abc",
            now=now,
        )
        await session.commit()

    assert task is not None
    assert task.kind == TaskKind.FETCH_VIDEO_STATS
    assert task.target_type == "video"
    assert task.target_id == "BV1abc"
    assert task.priority == 80
    assert task.not_before == datetime(2026, 7, 8, 10, 10, tzinfo=UTC)
    assert task.payload["reason"] == "snapshot_policy"
    assert "BV1abc" in str(task.idempotency_key)
    assert "2026-07-08T10:10:00+00:00" in str(task.idempotency_key)
    await engine.dispose()


@pytest.mark.asyncio
async def test_video_snapshot_scheduler_skips_unknown_video() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 7, tzinfo=UTC)
    async with session_factory() as session:
        task = await VideoSnapshotScheduler().schedule_next_for_video(
            session=session,
            bvid="BVunknown",
            now=now,
        )
        tasks = (await session.scalars(select(CollectionTask))).all()

    assert task is None
    assert tasks == []
    await engine.dispose()
