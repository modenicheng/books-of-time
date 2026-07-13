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
    EventVideo,
    KnownVideo,
    KnownVideoSource,
    RawPageObservation,
    RawPayload,
    RequestBackoffState,
)
from books_of_time.db.repositories import CollectionTaskRepository, EventRepository
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


async def _enqueue_discovery(
    session_factory,
    *,
    now: datetime,
    suffix: str,
    source_associations: list[dict] | None = None,
) -> None:
    async with session_factory() as session:
        payload = {
            "mid": "123",
            "page": 1,
            "source_pool_type": "game",
            "source_pool_id": "example-game",
            "reason": "scheduled_discovery",
        }
        if source_associations is not None:
            payload["source_associations"] = source_associations
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.DISCOVER_USER_VIDEOS,
            target_type="user",
            target_id="123",
            priority=110,
            payload=payload,
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
    second_seen = now + timedelta(seconds=1)
    client.captured_at = second_seen
    await _enqueue_discovery(
        session_factory,
        now=second_seen,
        suffix="second",
        source_associations=[
            {
                "source_mid": "123",
                "pool_type": "game",
                "pool_id": "example-game",
                "game_id": "example-game",
                "official": False,
                "monitored": True,
            },
            {
                "source_mid": "123",
                "pool_type": "event",
                "pool_id": "target:9",
                "game_id": None,
                "official": False,
                "monitored": True,
            },
        ],
    )
    assert await worker.run_once(now=second_seen) is True

    async with session_factory() as session:
        raw_payloads = list(await session.scalars(select(RawPayload)))
        raw_pages = list(await session.scalars(select(RawPageObservation)))
        known_videos = list(await session.scalars(select(KnownVideo)))
        known_sources = list(
            await session.scalars(
                select(KnownVideoSource).order_by(KnownVideoSource.pool_type)
            )
        )
        tasks = list(await session.scalars(select(CollectionTask)))
        coverage = list(await session.scalars(select(CollectionCoverageStat)))

    stats_tasks = [task for task in tasks if task.kind == TaskKind.FETCH_VIDEO_STATS]
    assert client.calls == [("123", 1), ("123", 1)]
    assert len(raw_payloads) == 2
    assert all(
        raw.parser_version == "bilibili-user-video-list-v2" for raw in raw_payloads
    )
    assert len(raw_pages) == 2
    assert all(page.target_type == "user" for page in raw_pages)
    assert all(page.target_id == "123" for page in raw_pages)
    assert all(page.item_count == 1 for page in raw_pages)
    assert [video.bvid for video in known_videos] == ["BV-DISCOVERED"]
    assert [source.pool_type for source in known_sources] == ["event", "game"]
    event_source, game_source = known_sources
    assert event_source.first_raw_page_id == raw_pages[1].id
    assert event_source.last_raw_page_id == raw_pages[1].id
    assert game_source.first_raw_page_id == raw_pages[0].id
    assert game_source.last_raw_page_id == raw_pages[1].id
    assert game_source.last_seen_at == second_seen
    assert len(stats_tasks) == 1
    assert stats_tasks[0].payload["source_pool_type"] == "game"
    assert stats_tasks[0].payload["source_pool_id"] == "example-game"
    assert stats_tasks[0].payload["source_associations"] == [
        {
            "source_mid": "123",
            "pool_type": "game",
            "pool_id": "example-game",
            "game_id": "example-game",
            "official": False,
            "monitored": True,
        }
    ]
    assert (
        raw_pages[0].extra["source_associations"]
        == (stats_tasks[0].payload["source_associations"])
    )
    assert len(raw_pages[1].extra["source_associations"]) == 2
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


@pytest.mark.asyncio
async def test_user_videos_worker_attaches_discovered_video_to_event(tmp_path) -> None:
    now = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)
    body = json.dumps(
        {
            "data": {
                "list": {
                    "vlist": [
                        {
                            "bvid": "BV1xx411c7mD",
                            "created": int(now.timestamp()),
                        }
                    ]
                }
            }
        }
    ).encode()
    engine, session_factory, worker = await _runtime(
        tmp_path,
        FakeUserVideosClient(body, now),
    )

    async with session_factory() as session:
        event_repository = EventRepository(session)
        event = await event_repository.create_event(
            slug="event-a",
            name="事件 A",
            now=now,
        )
        target = await event_repository.add_target(
            event_id=event.id,
            target_type="uid",
            target_value="123",
            now=now,
        )
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.DISCOVER_USER_VIDEOS,
            target_type="user",
            target_id="123",
            priority=110,
            payload={
                "mid": "123",
                "page": 1,
                "source_pool_type": "event",
                "source_pool_id": None,
                "event_links": [{"event_id": event.id, "target_id": target.id}],
            },
            not_before=now,
            idempotency_key="discover-user-videos:123:event-test",
        )
        await session.commit()

    assert await worker.run_once(now=now) is True

    async with session_factory() as session:
        event_video = await session.get(
            EventVideo,
            (event.id, "BV1xx411c7mD"),
        )
        stats_task = await session.scalar(
            select(CollectionTask).where(
                CollectionTask.kind == TaskKind.FETCH_VIDEO_STATS
            )
        )

    assert event_video is not None
    assert event_video.source_target_id == target.id
    assert event_video.association_reason == "uid_target"
    assert stats_task is not None
    assert stats_task.payload["event_links"] == [
        {"event_id": event.id, "target_id": target.id}
    ]
    async with session_factory() as session:
        raw_page = await session.scalar(select(RawPageObservation))
    assert raw_page is not None
    assert raw_page.extra["event_links"] == [
        {"event_id": event.id, "target_id": target.id}
    ]
    await engine.dispose()
