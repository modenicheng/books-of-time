import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.collectors.hot_comments import HotCommentCollector
from books_of_time.db.models import (
    Base,
    CollectionCoverageStat,
    CollectionTask,
    CommentEntity,
    CommentObservation,
    CommentObservationMedia,
    ImportantCommentWatchlist,
    MediaSource,
    RawPageObservation,
    RawPayload,
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
                    "aid": 777,
                    "bvid": bvid,
                    "stat": {
                        "view": 1,
                        "like": 1,
                        "coin": 0,
                        "favorite": 0,
                        "share": 0,
                        "reply": 1,
                        "danmaku": 0,
                    },
                },
            }
        ).encode()
        return FetchResult(
            request_type=BilibiliRequestType.VIDEO_STATS,
            method="GET",
            url="https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            status_code=200,
            body=body,
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        )

    async def get_hot_comments(self, *, aid: int, page: int = 1) -> FetchResult:
        body = json.dumps(
            {
                "code": 0,
                "data": {
                    "cursor": {"all_count": 1},
                    "replies": [
                        {
                            "rpid": 1001,
                            "oid": aid,
                            "root": 0,
                            "parent": 0,
                            "like": 12,
                            "rcount": 3,
                            "member": {"mid": "42", "uname": "Alice"},
                            "content": {
                                "message": "first comment",
                                "pictures": [
                                    {
                                        "img_src": "https://i0.hdslb.com/bfs/new_dyn/a.jpg"
                                    }
                                ],
                            },
                        }
                    ],
                },
            }
        ).encode()
        return FetchResult(
            request_type=BilibiliRequestType.COMMENT_HOT,
            method="GET",
            url="https://api.bilibili.com/x/v2/reply",
            params={"oid": aid, "pn": page, "sort": 2},
            status_code=200,
            body=body,
            captured_at=datetime(2026, 7, 8, 10, 1, tzinfo=UTC),
        )


class FakePagedHotCommentsClient:
    def __init__(self) -> None:
        self.pages: list[int] = []

    async def get_video_stats(self, bvid: str) -> FetchResult:
        raise AssertionError("aid is supplied in task payload")

    async def get_hot_comments(self, *, aid: int, page: int = 1) -> FetchResult:
        self.pages.append(page)
        rpid = 1000 + page
        body = json.dumps(
            {
                "code": 0,
                "data": {
                    "cursor": {"all_count": 2},
                    "replies": [
                        {
                            "rpid": rpid,
                            "oid": aid,
                            "root": 0,
                            "parent": 0,
                            "like": page,
                            "rcount": 0,
                            "member": {"mid": str(page), "uname": f"User {page}"},
                            "content": {"message": f"page {page} comment"},
                        }
                    ],
                },
            }
        ).encode()
        return FetchResult(
            request_type=BilibiliRequestType.COMMENT_HOT,
            method="GET",
            url="https://api.bilibili.com/x/v2/reply",
            params={"oid": aid, "pn": page, "sort": 2},
            status_code=200,
            body=body,
            captured_at=datetime(2026, 7, 8, 10, page, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_worker_fetch_hot_comments_archives_raw_and_writes_observations(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id="BV1abc",
            priority=80,
            payload={"bvid": "BV1abc", "mode": "hot", "page": 1},
            not_before=now - timedelta(seconds=1),
        )
        await session.commit()

    worker = Worker(
        session_factory=session_factory,
        collectors={
            TaskKind.FETCH_HOT_COMMENTS: HotCommentCollector(
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
        tasks = (
            await session.scalars(select(CollectionTask).order_by(CollectionTask.id))
        ).all()
        task = tasks[0]
        coverage = await session.scalar(select(CollectionCoverageStat))
        raw_payloads = (
            await session.scalars(select(RawPayload).order_by(RawPayload.id.asc()))
        ).all()
        raw_page = await session.scalar(select(RawPageObservation))
        entity = await session.scalar(select(CommentEntity))
        observation = await session.scalar(select(CommentObservation))
        watch = await session.scalar(select(ImportantCommentWatchlist))
        media_source = await session.scalar(select(MediaSource))
        media_link = await session.scalar(select(CommentObservationMedia))

        assert task.status == TaskStatus.SUCCEEDED
        assert len(tasks) == 2
        assert tasks[1].kind == TaskKind.FETCH_MEDIA_ASSET
        assert tasks[1].target_type == "media_source"
        assert tasks[1].payload["url"] == "https://i0.hdslb.com/bfs/new_dyn/a.jpg"
        assert coverage is not None
        assert coverage.task_kind == TaskKind.FETCH_HOT_COMMENTS
        assert coverage.status == "succeeded"
        assert coverage.reason == "complete"
        assert coverage.pages_requested == 1
        assert coverage.pages_succeeded == 1
        assert coverage.items_observed == 1
        assert coverage.raw_payloads_saved == 2
        assert coverage.truncated is False
        assert len(raw_payloads) == 2
        assert raw_payloads[0].request_type == BilibiliRequestType.VIDEO_STATS
        assert raw_payloads[1].request_type == BilibiliRequestType.COMMENT_HOT
        assert raw_page is not None
        assert raw_page.raw_payload_id == raw_payloads[1].id
        assert raw_page.target_id == "BV1abc"
        assert raw_page.sort_mode == "hot"
        assert raw_page.item_count == 1
        assert entity is not None
        assert entity.rpid == 1001
        assert entity.author_mid == 42
        assert entity.author_name == "Alice"
        assert observation is not None
        assert observation.rpid == 1001
        assert observation.raw_payload_id == raw_payloads[1].id
        assert observation.raw_page_observation_id == raw_page.id
        assert observation.content == "first comment"
        assert watch is not None
        assert watch.rpid == 1001
        assert watch.reason == "hot_top"
        assert watch.hot_position == 1
        assert watch.priority >= 90
        assert media_source is not None
        assert media_source.fetch_status == "pending"
        assert media_link is not None
        assert media_link.comment_observation_id == observation.id
        assert media_link.media_source_id == media_source.id
        assert media_link.position == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_fetch_hot_comments_collects_configured_page_limit(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id="BV1abc",
            priority=80,
            payload={
                "bvid": "BV1abc",
                "aid": 777,
                "mode": "hot",
                "page": 1,
                "page_limit": 2,
            },
            not_before=now - timedelta(seconds=1),
        )
        await session.commit()

    client = FakePagedHotCommentsClient()
    worker = Worker(
        session_factory=session_factory,
        collectors={
            TaskKind.FETCH_HOT_COMMENTS: HotCommentCollector(
                client=client,
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
        coverage = await session.scalar(select(CollectionCoverageStat))
        raw_pages = (
            await session.scalars(
                select(RawPageObservation).order_by(RawPageObservation.page_number)
            )
        ).all()
        observations = (
            await session.scalars(
                select(CommentObservation).order_by(CommentObservation.rpid)
            )
        ).all()
        raw_payloads = (await session.scalars(select(RawPayload))).all()

        assert client.pages == [1, 2]
        assert coverage is not None
        assert coverage.pages_requested == 2
        assert coverage.pages_succeeded == 2
        assert coverage.items_observed == 2
        assert coverage.raw_payloads_saved == 2
        assert [page.page_number for page in raw_pages] == [1, 2]
        assert [observation.rpid for observation in observations] == [1001, 1002]
        assert len(raw_payloads) == 2

    await engine.dispose()
