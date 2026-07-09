import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.collectors.reply_comments import ReplyCommentCollector
from books_of_time.db.models import (
    Base,
    CollectionCoverageStat,
    CollectionTask,
    CommentObservation,
    RawPageObservation,
    RawPayload,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.http.client import FetchResult
from books_of_time.storage.filesystem import RawPayloadFileStore
from books_of_time.worker import Worker


class FakeReplyClient:
    async def get_video_stats(self, bvid: str) -> FetchResult:
        body = json.dumps({"code": 0, "data": {"aid": 777, "bvid": bvid}}).encode()
        return FetchResult(
            request_type=BilibiliRequestType.VIDEO_STATS,
            method="GET",
            url="https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            status_code=200,
            body=body,
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        )

    async def get_comment_replies(
        self,
        *,
        aid: int,
        root_rpid: int,
        page: int = 1,
        page_size: int = 20,
    ) -> FetchResult:
        body = json.dumps(
            {
                "code": 0,
                "data": {
                    "page": {"num": page, "size": page_size, "count": 1},
                    "replies": [
                        {
                            "rpid": 3001,
                            "oid": aid,
                            "root": root_rpid,
                            "parent": root_rpid,
                            "like": 4,
                            "rcount": 0,
                            "member": {"mid": "43", "uname": "Carol"},
                            "content": {"message": "sub reply"},
                        }
                    ],
                },
            }
        ).encode()
        return FetchResult(
            request_type=BilibiliRequestType.COMMENT_REPLY,
            method="GET",
            url="https://api.bilibili.com/x/v2/reply/reply",
            params={"oid": aid, "root": root_rpid, "pn": page, "ps": page_size},
            status_code=200,
            body=body,
            captured_at=datetime(2026, 7, 8, 10, page, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_worker_fetch_comment_replies_writes_observations_and_coverage(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_COMMENT_REPLIES,
            target_type="comment",
            target_id="1001",
            priority=90,
            payload={
                "bvid": "BV1abc",
                "aid": 777,
                "root_rpid": 1001,
                "page": 1,
                "page_limit": 1,
                "page_size": 20,
            },
            not_before=now - timedelta(seconds=1),
        )
        await session.commit()

    worker = Worker(
        session_factory=session_factory,
        collectors={
            TaskKind.FETCH_COMMENT_REPLIES: ReplyCommentCollector(
                client=FakeReplyClient(),
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
        raw_page = await session.scalar(select(RawPageObservation))
        observation = await session.scalar(select(CommentObservation))
        raw_payload = await session.scalar(select(RawPayload))

        assert task is not None
        assert task.status == TaskStatus.SUCCEEDED
        assert coverage is not None
        assert coverage.task_kind == TaskKind.FETCH_COMMENT_REPLIES
        assert coverage.pages_requested == 1
        assert coverage.pages_succeeded == 1
        assert coverage.items_observed == 1
        assert coverage.extra["reply_roots_requested"] == 1
        assert coverage.extra["reply_roots_succeeded"] == 1
        assert raw_payload is not None
        assert raw_payload.request_type == BilibiliRequestType.COMMENT_REPLY
        assert raw_page is not None
        assert raw_page.sort_mode == "reply"
        assert raw_page.extra["root_rpid"] == 1001
        assert observation is not None
        assert observation.rpid == 3001
        assert observation.content == "sub reply"
        assert observation.raw_page_observation_id == raw_page.id

    await engine.dispose()
