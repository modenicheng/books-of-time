import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.collectors.latest_comments import LatestCommentCollector
from books_of_time.db.models import (
    Base,
    CollectionTask,
    CommentEntity,
    CommentObservation,
    FrontierState,
    RawPageObservation,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.http.client import FetchResult
from books_of_time.storage.filesystem import RawPayloadFileStore
from books_of_time.worker import Worker


def latest_body(
    *,
    rpid: int | None,
    next_offset: str,
    is_end: bool = False,
) -> bytes:
    replies = []
    if rpid is not None:
        replies = [
            {
                "rpid": rpid,
                "oid": 777,
                "root": 0,
                "parent": 0,
                "like": rpid % 10,
                "rcount": 0,
                "member": {"mid": str(rpid), "uname": f"User {rpid}"},
                "content": {"message": f"comment {rpid}"},
            }
        ]
    return json.dumps(
        {
            "code": 0,
            "data": {
                "cursor": {
                    "pagination_reply": {"next_offset": next_offset},
                    "is_end": is_end,
                },
                "replies": replies,
            },
        }
    ).encode()


class FakeLatestClient:
    def __init__(
        self,
        pages: dict[str, bytes],
        failures: dict[str, list[Exception]] | None = None,
        video_stats_effect=None,
    ) -> None:
        self.pages = pages
        self.failures = failures or {}
        self.video_stats_effect = video_stats_effect
        self.latest_offsets: list[str] = []
        self.video_stats_calls = 0

    async def get_video_stats(self, bvid: str) -> FetchResult:
        self.video_stats_calls += 1
        if self.video_stats_effect is not None:
            self.video_stats_effect()
        return FetchResult(
            request_type=BilibiliRequestType.VIDEO_STATS,
            method="GET",
            url="https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            status_code=200,
            body=json.dumps({"code": 0, "data": {"aid": 777, "bvid": bvid}}).encode(),
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        )

    async def get_latest_comments(self, *, aid: int, offset: str = "") -> FetchResult:
        self.latest_offsets.append(offset)
        queued_failures = self.failures.get(offset, [])
        if queued_failures:
            raise queued_failures.pop(0)
        return FetchResult(
            request_type=BilibiliRequestType.COMMENT_LATEST,
            method="GET",
            url="https://api.bilibili.com/x/v2/reply/wbi/main",
            params={"oid": aid, "mode": 2, "pagination_str": offset},
            status_code=200,
            body=self.pages[offset],
            captured_at=datetime(2026, 7, 8, 10, len(self.latest_offsets), tzinfo=UTC),
        )


class ManualClock:
    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.index = 0

    def monotonic(self) -> float:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


async def build_worker_with_task(
    tmp_path,
    client,
    *,
    max_scan_seconds: float = 55,
    page_retry_attempts: int = 3,
    page_retry_backoff_seconds: list[float] | None = None,
    clock=None,
    sleep=None,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type="video",
            target_id="BV1abc",
            priority=70,
            payload={"bvid": "BV1abc", "mode": "latest"},
            not_before=now - timedelta(seconds=1),
        )
        await session.commit()

    collector = LatestCommentCollector(
        client=client,
        raw_store=RawPayloadFileStore(tmp_path),
        run_id="test-run",
        max_scan_seconds=max_scan_seconds,
        page_retry_attempts=page_retry_attempts,
        page_retry_backoff_seconds=page_retry_backoff_seconds or [0, 0, 0],
        monotonic=clock.monotonic if clock else None,
        sleep=sleep or (lambda seconds: None),
    )
    worker = Worker(
        session_factory=session_factory,
        collectors={TaskKind.FETCH_LATEST_COMMENTS: collector},
        lease_owner="worker-test",
    )
    return engine, session_factory, worker, now


@pytest.mark.asyncio
async def test_baseline_pauses_at_time_budget_and_enqueues_followup(tmp_path) -> None:
    client = FakeLatestClient(
        {
            "": latest_body(rpid=3003, next_offset="offset-2"),
            "offset-2": latest_body(rpid=3002, next_offset="offset-3"),
        }
    )
    clock = ManualClock([0, 0, 0, 60, 60])
    engine, session_factory, worker, now = await build_worker_with_task(
        tmp_path,
        client,
        max_scan_seconds=55,
        clock=clock,
    )

    executed = await worker.run_once(now=now)
    assert executed is True

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))
        tasks = (
            await session.scalars(
                select(CollectionTask).order_by(CollectionTask.id.asc())
            )
        ).all()

        assert state is not None
        assert state.last_scan_status == "baseline_paused"
        assert state.last_scan_truncated is True
        assert state.cursor == "offset-2"
        assert state.extra["baseline_start_frontier_rpid"] == 3003
        assert state.extra["baseline_status"] == "baseline_paused"
        assert [task.kind for task in tasks] == [
            TaskKind.FETCH_LATEST_COMMENTS,
            TaskKind.FETCH_LATEST_COMMENTS,
        ]
        assert tasks[1].status == TaskStatus.PENDING
        assert tasks[1].payload["bvid"] == "BV1abc"
        assert tasks[1].payload["mode"] == "latest"

    await engine.dispose()


@pytest.mark.asyncio
async def test_collect_pauses_before_latest_request_when_video_stats_uses_budget(
    tmp_path,
) -> None:
    clock = ManualClock([0, 60, 60])
    client = FakeLatestClient(
        {"": latest_body(rpid=3003, next_offset="offset-2")},
        video_stats_effect=clock.monotonic,
    )
    engine, session_factory, worker, now = await build_worker_with_task(
        tmp_path,
        client,
        max_scan_seconds=55,
        clock=clock,
    )

    executed = await worker.run_once(now=now)
    assert executed is True

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))
        tasks = (
            await session.scalars(
                select(CollectionTask).order_by(CollectionTask.id.asc())
            )
        ).all()

        assert state is not None
        assert state.last_scan_status == "baseline_paused"
        assert state.last_scan_truncated is True
        assert state.cursor == ""
        assert state.extra["baseline_status"] == "baseline_paused"
        assert "failed_cursor" not in state.extra
        assert "failed_reason" not in state.extra
        assert "failed_attempts" not in state.extra
        assert [task.kind for task in tasks] == [
            TaskKind.FETCH_LATEST_COMMENTS,
            TaskKind.FETCH_LATEST_COMMENTS,
        ]
        assert tasks[1].status == TaskStatus.PENDING

    assert client.video_stats_calls == 1
    assert client.latest_offsets == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_baseline_resumes_from_saved_cursor_and_marks_tail_complete(
    tmp_path,
) -> None:
    client = FakeLatestClient(
        {
            "": latest_body(rpid=3003, next_offset="offset-2"),
            "offset-2": latest_body(rpid=3002, next_offset="", is_end=True),
        }
    )
    engine, session_factory, worker, now = await build_worker_with_task(
        tmp_path, client
    )

    await worker.run_once(now=now)

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))
        raw_pages = (
            await session.scalars(
                select(RawPageObservation).order_by(RawPageObservation.id.asc())
            )
        ).all()
        entity_count = await session.scalar(select(func.count(CommentEntity.rpid)))
        observation_count = await session.scalar(
            select(func.count(CommentObservation.id))
        )

        assert state is not None
        assert state.last_scan_status == "baseline_tail_complete"
        assert state.last_scan_truncated is False
        assert state.cursor == ""
        assert state.extra["baseline_status"] == "baseline_tail_complete"
        assert state.extra["baseline_start_frontier_rpid"] == 3003
        assert [page.cursor for page in raw_pages] == ["", "offset-2"]
        assert entity_count == 2
        assert observation_count == 2

    await engine.dispose()


@pytest.mark.asyncio
async def test_baseline_corrupted_when_same_cursor_fails_after_attempts(
    tmp_path,
) -> None:
    client = FakeLatestClient(
        {"": latest_body(rpid=3003, next_offset="offset-2")},
        failures={"": [RuntimeError("network down"), RuntimeError("still down")]},
    )
    engine, session_factory, worker, now = await build_worker_with_task(
        tmp_path,
        client,
        page_retry_attempts=2,
    )

    await worker.run_once(now=now)

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))
        task = await session.scalar(select(CollectionTask))

        assert task is not None
        assert task.status == TaskStatus.SUCCEEDED
        assert state is not None
        assert state.last_scan_status == "baseline_corrupted"
        assert state.last_scan_truncated is True
        assert state.extra["baseline_status"] == "baseline_corrupted"
        assert state.extra["failed_cursor"] == ""
        assert state.extra["failed_attempts"] == 2
        assert "still down" in state.extra["failed_reason"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_cursor_pauses_when_time_slice_expires_before_attempts_exhausted(
    tmp_path,
) -> None:
    client = FakeLatestClient(
        {"": latest_body(rpid=3003, next_offset="offset-2")},
        failures={"": [RuntimeError("temporary down")]},
    )
    clock = ManualClock([0, 0, 60, 60])
    engine, session_factory, worker, now = await build_worker_with_task(
        tmp_path,
        client,
        max_scan_seconds=55,
        page_retry_attempts=3,
        clock=clock,
    )

    await worker.run_once(now=now)

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))

        assert state is not None
        assert state.last_scan_status == "baseline_paused"
        assert state.last_scan_truncated is True
        assert state.cursor == ""
        assert state.extra["failed_cursor"] == ""
        assert state.extra["failed_attempts"] == 1
        assert "temporary down" in state.extra["failed_reason"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_cursor_resumes_retry_on_next_run_without_repeat_corruption(
    tmp_path,
) -> None:
    client = FakeLatestClient(
        {
            "": latest_body(rpid=3003, next_offset="offset-2"),
            "offset-2": latest_body(rpid=3002, next_offset="", is_end=True),
        },
        failures={"": [RuntimeError("temporary down")]},
    )
    first_run_clock = ManualClock([0, 0, 56, 56])
    engine, session_factory, worker, now = await build_worker_with_task(
        tmp_path,
        client,
        max_scan_seconds=55,
        page_retry_attempts=3,
        clock=first_run_clock,
    )

    await worker.run_once(now=now)

    async with session_factory() as session:
        paused_state = await session.scalar(select(FrontierState))

        assert paused_state is not None
        assert paused_state.last_scan_status == "baseline_paused"
        assert paused_state.extra["failed_cursor"] == ""
        assert paused_state.extra["failed_attempts"] == 1

    second_run_collector = LatestCommentCollector(
        client=client,
        raw_store=RawPayloadFileStore(tmp_path),
        run_id="test-run-2",
        max_scan_seconds=55,
        page_retry_attempts=3,
        page_retry_backoff_seconds=[0, 0, 0],
        sleep=lambda seconds: None,
    )
    second_run_worker = Worker(
        session_factory=session_factory,
        collectors={TaskKind.FETCH_LATEST_COMMENTS: second_run_collector},
        lease_owner="worker-test-2",
    )

    executed = await second_run_worker.run_once(now=datetime(2099, 1, 1, tzinfo=UTC))
    assert executed is True

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))
        raw_pages = (
            await session.scalars(
                select(RawPageObservation).order_by(RawPageObservation.id.asc())
            )
        ).all()

        assert state is not None
        assert state.last_scan_status == "baseline_tail_complete"
        assert state.last_scan_truncated is False
        assert state.cursor == ""
        assert state.extra["baseline_status"] == "baseline_tail_complete"
        assert state.extra.get("failed_cursor") is None
        assert [page.cursor for page in raw_pages] == ["", "offset-2"]

    assert client.latest_offsets == ["", "", "offset-2"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_retry_backoff_pauses_when_sleep_would_exceed_remaining_budget(
    tmp_path,
) -> None:
    client = FakeLatestClient(
        {"": latest_body(rpid=3003, next_offset="offset-2")},
        failures={"": [RuntimeError("temporary down")]},
    )
    sleep_calls: list[float] = []
    clock = ManualClock([0, 0, 50, 56])
    engine, session_factory, worker, now = await build_worker_with_task(
        tmp_path,
        client,
        max_scan_seconds=55,
        page_retry_attempts=3,
        page_retry_backoff_seconds=[10, 10, 10],
        clock=clock,
        sleep=sleep_calls.append,
    )

    await worker.run_once(now=now)

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))

        assert state is not None
        assert state.last_scan_status == "baseline_paused"
        assert state.last_scan_truncated is True
        assert state.cursor == ""
        assert state.extra["failed_cursor"] == ""
        assert state.extra["failed_attempts"] == 1
        assert "temporary down" in state.extra["failed_reason"]

    assert sleep_calls == []

    await engine.dispose()
