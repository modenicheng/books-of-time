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


@pytest.mark.asyncio
async def test_video_snapshot_scheduler_enqueues_daily_terminal_snapshot_once() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    terminal_at = datetime(2026, 7, 8, 14, 0, tzinfo=UTC)  # 22:00 Asia/Shanghai
    async with session_factory() as session:
        session.add_all(
            [
                KnownVideo(
                    bvid="BV1",
                    source_mid="123",
                    pubdate=terminal_at - timedelta(hours=1),
                    first_seen_at=terminal_at - timedelta(minutes=30),
                ),
                KnownVideo(
                    bvid="BV2",
                    source_mid="123",
                    pubdate=terminal_at - timedelta(minutes=10),
                    first_seen_at=terminal_at - timedelta(minutes=5),
                ),
            ]
        )
        await session.commit()

        first_result = await VideoSnapshotScheduler().schedule_terminal_snapshots(
            session=session,
            now=terminal_at,
        )
        second_result = await VideoSnapshotScheduler().schedule_terminal_snapshots(
            session=session,
            now=terminal_at + timedelta(minutes=1),
        )
        await session.commit()

    async with session_factory() as session:
        tasks = (
            await session.scalars(select(CollectionTask).order_by(CollectionTask.id))
        ).all()

        assert len(first_result) == 2
        assert len(second_result) == 2
        assert len(tasks) == 2
        assert [task.target_id for task in tasks] == ["BV1", "BV2"]
        assert [task.not_before for task in tasks] == [terminal_at, terminal_at]
        assert [task.payload["reason"] for task in tasks] == [
            "daily_terminal_snapshot",
            "daily_terminal_snapshot",
        ]
        assert all("terminal:2026-07-08" in str(task.idempotency_key) for task in tasks)

    await engine.dispose()


@pytest.mark.asyncio
async def test_video_snapshot_scheduler_skips_terminal_snapshot_before_stop_hour() -> (
    None
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    before_terminal = datetime(2026, 7, 8, 13, 59, tzinfo=UTC)  # 21:59 Asia/Shanghai
    async with session_factory() as session:
        session.add(
            KnownVideo(
                bvid="BV1",
                source_mid="123",
                pubdate=before_terminal - timedelta(hours=1),
                first_seen_at=before_terminal - timedelta(minutes=30),
            )
        )
        await session.commit()

        result = await VideoSnapshotScheduler().schedule_terminal_snapshots(
            session=session,
            now=before_terminal,
        )
        await session.commit()

    async with session_factory() as session:
        tasks = (await session.scalars(select(CollectionTask))).all()

        assert result == []
        assert tasks == []

    await engine.dispose()
