import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.collectors.video_stats import VideoStatsCollector
from books_of_time.db.models import (
    Base,
    CollectionCoverageStat,
    CollectionTask,
    RawPayload,
    VideoMetricSnapshot,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.http.client import FetchResult
from books_of_time.storage.filesystem import RawPayloadFileStore
from books_of_time.worker import Worker


class FakeBilibiliClient:
    async def get_video_stats(self, bvid: str) -> FetchResult:
        body = json.dumps(
            {
                "code": 0,
                "data": {
                    "bvid": bvid,
                    "view": 1234,
                    "like": 234,
                    "coin": 34,
                    "favorite": 45,
                    "share": 6,
                    "reply": 78,
                    "danmaku": 90,
                },
            }
        ).encode()
        return FetchResult(
            request_type=BilibiliRequestType.VIDEO_STATS,
            method="GET",
            url="https://api.bilibili.com/x/web-interface/archive/stat",
            params={"bvid": bvid},
            status_code=200,
            body=body,
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_worker_fetch_video_stats_archives_raw_then_writes_snapshot(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        task_repo = CollectionTaskRepository(session)
        await task_repo.enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BV1abc",
            priority=100,
            payload={"bvid": "BV1abc"},
            not_before=now - timedelta(seconds=1),
        )
        await session.commit()

    worker = Worker(
        session_factory=session_factory,
        collectors={
            TaskKind.FETCH_VIDEO_STATS: VideoStatsCollector(
                client=FakeBilibiliClient(),
                raw_store=RawPayloadFileStore(tmp_path),
                run_id="test-run",
            )
        },
        run_id="test-run",
        lease_owner="worker-test",
    )

    executed = await worker.run_once(now=now)
    assert executed is True

    async with session_factory() as session:
        task = await session.scalar(select(CollectionTask))
        coverage = await session.scalar(select(CollectionCoverageStat))
        raw = await session.scalar(select(RawPayload))
        snapshot = await session.scalar(select(VideoMetricSnapshot))

        assert task.status == TaskStatus.SUCCEEDED
        assert coverage is not None
        assert coverage.task_kind == TaskKind.FETCH_VIDEO_STATS
        assert coverage.status == "succeeded"
        assert coverage.reason == "complete"
        assert coverage.pages_requested == 1
        assert coverage.pages_succeeded == 1
        assert coverage.items_observed == 1
        assert coverage.raw_payloads_saved == 1
        assert raw is not None
        assert raw.request_type == BilibiliRequestType.VIDEO_STATS
        assert snapshot is not None
        assert snapshot.bvid == "BV1abc"
        assert snapshot.view_count == 1234
        assert snapshot.raw_payload_id == raw.id

    await engine.dispose()
