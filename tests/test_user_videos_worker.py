from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.app import build_worker
from books_of_time.db.base import Base
from books_of_time.db.models import (
    CollectionCoverageStat,
    CollectionTask,
    KnownVideo,
    RawPageObservation,
    RawPayload,
    RequestBackoffState,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.http.client import FetchResult


class FakeUserVideosClient:
    def __init__(self, body: bytes, captured_at: datetime) -> None:
        self.body = body
        self.captured_at = captured_at
        self.calls: list[tuple[str, int]] = []
        self.http_client = object()
        self.rate_limiter = None

    async def get_user_video_list(self, mid: str, page: int = 1) -> FetchResult:
        self.calls.append((mid, page))
        return FetchResult(
            request_type=BilibiliRequestType.USER_VIDEO_LIST,
            method="GET",
            url="https://api.bilibili.com/x/space/wbi/arc/search",
            params={"mid": mid, "pn": page},
            status_code=200,
            body=self.body,
            captured_at=self.captured_at,
            response_headers={},
        )


async def _runtime(tmp_path, client):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    cfg = {
        "database": {"url": "sqlite+aiosqlite:///:memory:"},
        "storage": {
            "raw_dir": str(tmp_path / "raw"),
            "media_dir": str(tmp_path / "media"),
        },
    }
    worker = build_worker(
        cfg,
        run_id="user-videos-test",
        lease_owner="worker-user-videos",
        session_factory=session_factory,
        client=client,
    )
    return engine, session_factory, worker


async def _enqueue_discovery(session_factory, *, now: datetime, suffix: str) -> None:
    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.DISCOVER_USER_VIDEOS,
            target_type="user",
            target_id="123",
            priority=110,
            payload={
                "mid": "123",
                "page": 1,
                "source_pool_type": "game",
                "source_pool_id": "example-game",
                "reason": "scheduled_discovery",
            },
            not_before=now,
            idempotency_key=f"discover-user-videos:123:{suffix}",
        )
        await session.commit()


@pytest.mark.asyncio
async def test_user_videos_worker_archives_page_and_deduplicates_discovery(
    tmp_path,
) -> None:
    now = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)
    body = json.dumps(
        {
            "data": {
                "list": {
                    "vlist": [
                        {
                            "bvid": "BV-DISCOVERED",
                            "created": int((now - timedelta(seconds=30)).timestamp()),
                        }
                    ]
                }
            }
        }
    ).encode()
    client = FakeUserVideosClient(body, now)
    engine, session_factory, worker = await _runtime(tmp_path, client)

    await _enqueue_discovery(session_factory, now=now, suffix="first")
    assert await worker.run_once(now=now) is True
    await _enqueue_discovery(
        session_factory,
        now=now + timedelta(seconds=1),
        suffix="second",
    )
    assert await worker.run_once(now=now + timedelta(seconds=1)) is True

    async with session_factory() as session:
        raw_payloads = list(await session.scalars(select(RawPayload)))
        raw_pages = list(await session.scalars(select(RawPageObservation)))
        known_videos = list(await session.scalars(select(KnownVideo)))
        tasks = list(await session.scalars(select(CollectionTask)))
        coverage = list(await session.scalars(select(CollectionCoverageStat)))

    stats_tasks = [task for task in tasks if task.kind == TaskKind.FETCH_VIDEO_STATS]
    assert client.calls == [("123", 1), ("123", 1)]
    assert len(raw_payloads) == 2
    assert all(
        raw.parser_version == "bilibili-user-video-list-v1" for raw in raw_payloads
    )
    assert len(raw_pages) == 2
    assert all(page.target_type == "user" for page in raw_pages)
    assert all(page.target_id == "123" for page in raw_pages)
    assert all(page.item_count == 1 for page in raw_pages)
    assert [video.bvid for video in known_videos] == ["BV-DISCOVERED"]
    assert len(stats_tasks) == 1
    assert stats_tasks[0].payload["source_pool_type"] == "game"
    assert stats_tasks[0].payload["source_pool_id"] == "example-game"
    assert len(coverage) == 2
    assert coverage[0].items_observed == 1
    assert coverage[0].raw_payloads_saved == 1
    assert coverage[0].extra["videos_created"] == 1
    assert coverage[1].extra["videos_created"] == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_user_videos_worker_preserves_raw_on_parse_failure(tmp_path) -> None:
    now = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)
    client = FakeUserVideosClient(b"not-json", now)
    engine, session_factory, worker = await _runtime(tmp_path, client)
    await _enqueue_discovery(session_factory, now=now, suffix="malformed")

    assert await worker.run_once(now=now) is True

    async with session_factory() as session:
        raw_count = await session.scalar(select(func.count(RawPayload.id)))
        coverage = await session.scalar(select(CollectionCoverageStat))
        backoff = await session.scalar(select(RequestBackoffState))
        discovery_task = await session.scalar(
            select(CollectionTask).where(
                CollectionTask.kind == TaskKind.DISCOVER_USER_VIDEOS
            )
        )

    assert raw_count == 1
    assert coverage is not None
    assert coverage.reason == "parse_error"
    assert coverage.status == "failed"
    assert backoff is not None
    assert backoff.request_type == BilibiliRequestType.USER_VIDEO_LIST
    assert discovery_task is not None
    assert discovery_task.status == TaskStatus.PENDING
    assert discovery_task.retry_count == 1
    await engine.dispose()
