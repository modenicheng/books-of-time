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
    RequestBackoffState,
    VideoInfoSnapshot,
    VideoMetricSnapshot,
)
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    VideoInfoSnapshotRepository,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.http.client import FetchResult
from books_of_time.http.errors import ParseFailure
from books_of_time.parsers.video import parse_video_info_snapshot
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
                    "title": "Demo Video",
                    "desc": "A useful description",
                    "owner": {"mid": 12345, "name": "Example UP"},
                    "tag": [{"tag_name": "攻略"}, {"name": "游戏"}],
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


class FakeMalformedBilibiliClient:
    async def get_video_stats(self, bvid: str) -> FetchResult:
        return FetchResult(
            request_type=BilibiliRequestType.VIDEO_STATS,
            method="GET",
            url="https://api.bilibili.com/x/web-interface/archive/stat",
            params={"bvid": bvid},
            status_code=200,
            body=b'{"code":0,"data":{}}',
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_video_info_snapshot_repository_inserts_parsed_snapshot() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    parsed = parse_video_info_snapshot(
        {
            "code": 0,
            "data": {
                "bvid": "BV1abc",
                "title": "Demo Video",
                "owner": {"mid": 12345, "name": "Example UP"},
                "tag": ["攻略"],
            },
        },
        captured_at=captured_at,
        raw_payload_id=42,
    )

    async with session_factory() as session:
        row = await VideoInfoSnapshotRepository(session).insert_from_parsed(parsed)
        await session.commit()

        stored = await session.get(VideoInfoSnapshot, ("BV1abc", captured_at))

    assert row.bvid == "BV1abc"
    assert stored is not None
    assert stored.title == "Demo Video"
    assert stored.owner_mid == 12345
    assert stored.tags == {"names": ["攻略"], "source_fields": ["tag"]}
    assert stored.raw_payload_id == 42
    await engine.dispose()


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
        info_snapshot = await session.scalar(select(VideoInfoSnapshot))

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
        assert info_snapshot is not None
        assert info_snapshot.bvid == "BV1abc"
        assert info_snapshot.title == "Demo Video"
        assert info_snapshot.description == "A useful description"
        assert info_snapshot.owner_mid == 12345
        assert info_snapshot.owner_name == "Example UP"
        assert info_snapshot.tags["names"] == ["攻略", "游戏"]
        assert info_snapshot.raw_payload_id == raw.id

    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_records_video_stats_parse_error_as_request_backoff(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
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
                client=FakeMalformedBilibiliClient(),
                raw_store=RawPayloadFileStore(tmp_path),
                run_id="test-run",
            )
        },
        run_id="test-run",
        lease_owner="worker-test",
        request_backoff_defaults={"parse_error": 30},
    )

    with pytest.raises(ParseFailure):
        await worker.run_once(now=now)

    async with session_factory() as session:
        coverage = await session.scalar(select(CollectionCoverageStat))
        backoff = await session.scalar(select(RequestBackoffState))
        task = await session.scalar(select(CollectionTask))

        assert coverage is not None
        assert coverage.status == "failed"
        assert coverage.reason == "parse_error"
        assert backoff is not None
        assert backoff.error_kind == "parse_error"
        assert backoff.backoff_until == now + timedelta(seconds=30)
        assert task is not None
        assert task.status == TaskStatus.PENDING
        assert task.not_before == now + timedelta(seconds=30)

    await engine.dispose()
