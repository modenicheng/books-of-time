from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import VideoMetricSnapshot
from books_of_time.db.repositories import VideoMetricSnapshotRepository
from books_of_time.task_orchestrator.video_snapshot_policy import (
    get_next_video_snapshot_at,
)


def _snapshot(
    *,
    bvid: str,
    captured_at: datetime,
    view_count: int | None,
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
async def test_video_metric_repository_computes_view_growth_from_pre_cutoff_baseline() -> (
    None
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 11, 0, tzinfo=UTC)
    async with session_factory() as session:
        session.add_all(
            [
                _snapshot(
                    bvid="BV1abc",
                    captured_at=now - timedelta(minutes=70),
                    view_count=100,
                ),
                _snapshot(
                    bvid="BV1abc",
                    captured_at=now - timedelta(minutes=30),
                    view_count=600,
                ),
                _snapshot(
                    bvid="BV1abc",
                    captured_at=now - timedelta(minutes=5),
                    view_count=1100,
                ),
            ]
        )
        await session.commit()

        growth = await VideoMetricSnapshotRepository(session).get_view_growth_since(
            bvid="BV1abc",
            since=now - timedelta(hours=1),
            now=now,
        )

    assert growth == 1000
    await engine.dispose()


@pytest.mark.asyncio
async def test_video_metric_repository_uses_oldest_in_window_without_pre_cutoff_baseline() -> (
    None
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 11, 0, tzinfo=UTC)
    async with session_factory() as session:
        session.add_all(
            [
                _snapshot(
                    bvid="BV1abc",
                    captured_at=now - timedelta(minutes=45),
                    view_count=400,
                ),
                _snapshot(
                    bvid="BV1abc",
                    captured_at=now - timedelta(minutes=5),
                    view_count=900,
                ),
            ]
        )
        await session.commit()

        growth = await VideoMetricSnapshotRepository(session).get_view_growth_since(
            bvid="BV1abc",
            since=now - timedelta(hours=1),
            now=now,
        )

    assert growth == 500
    await engine.dispose()


@pytest.mark.asyncio
async def test_video_metric_repository_clamps_negative_view_growth() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 11, 0, tzinfo=UTC)
    async with session_factory() as session:
        session.add_all(
            [
                _snapshot(
                    bvid="BV1abc",
                    captured_at=now - timedelta(minutes=70),
                    view_count=1000,
                ),
                _snapshot(
                    bvid="BV1abc",
                    captured_at=now - timedelta(minutes=5),
                    view_count=900,
                ),
            ]
        )
        await session.commit()

        growth = await VideoMetricSnapshotRepository(session).get_view_growth_since(
            bvid="BV1abc",
            since=now - timedelta(hours=1),
            now=now,
        )

    assert growth == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_next_video_snapshot_at_uses_stored_view_growth() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    published_at = datetime(2026, 7, 8, 3, 0, tzinfo=UTC)
    now = datetime(2026, 7, 8, 10, 7, tzinfo=UTC)
    async with session_factory() as session:
        session.add_all(
            [
                _snapshot(
                    bvid="BV1abc",
                    captured_at=now - timedelta(minutes=70),
                    view_count=1000,
                ),
                _snapshot(
                    bvid="BV1abc",
                    captured_at=now - timedelta(minutes=5),
                    view_count=33_000,
                ),
            ]
        )
        await session.commit()

        next_at = await get_next_video_snapshot_at(
            session,
            bvid="BV1abc",
            published_at=published_at,
            now=now,
        )

    assert next_at == datetime(2026, 7, 8, 10, 10, tzinfo=UTC)
    await engine.dispose()
