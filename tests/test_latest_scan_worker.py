from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.collectors.latest_comments import LatestCommentCollector
from books_of_time.db.base import Base
from books_of_time.db.latest_scan_repositories import (
    LatestScanRunPlan,
    LatestScanRunRepository,
)
from books_of_time.db.models import (
    CollectionPolicyVersion,
    CollectionTask,
    CommentObservation,
    CommentObservationMedia,
    CommentScanRun,
    FrontierState,
    KnownVideo,
    RawPageObservation,
    RawPayload,
)
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    FrontierStateRepository,
    FrontierStateUpdate,
    FrontierVersionConflict,
)
from books_of_time.domain.cohort_policy import CohortRolloutMode
from books_of_time.domain.enums import (
    BilibiliRequestType,
    CommentScanMode,
    CommentScanStatus,
    TaskKind,
)
from books_of_time.http.client import FetchResult
from books_of_time.storage.filesystem import RawPayloadFileStore


def latest_body(
    *,
    rpids: list[int],
    next_offset: str,
    is_end: bool = False,
    media_url: str | None = None,
) -> bytes:
    replies = []
    for index, rpid in enumerate(rpids):
        content: dict[str, object] = {"message": f"comment {rpid}"}
        if index == 0 and media_url is not None:
            content["pictures"] = [{"img_src": media_url}]
        replies.append(
            {
                "rpid": rpid,
                "oid": 777,
                "root": 0,
                "parent": 0,
                "like": rpid % 10,
                "rcount": 0,
                "ctime": 1_700_000_000 + rpid,
                "member": {"mid": str(rpid), "uname": f"User {rpid}"},
                "content": content,
            }
        )
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


class MutableClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value


class FakeLatestClient:
    def __init__(
        self,
        pages: dict[str, bytes],
        *,
        failures: dict[str, list[Exception]] | None = None,
        clock: MutableClock | None = None,
        advance_after_offsets: set[str] | None = None,
    ) -> None:
        self.pages = pages
        self.failures = failures or {}
        self.clock = clock
        self.advance_after_offsets = advance_after_offsets or set()
        self.latest_offsets: list[str] = []
        self.video_stats_calls = 0

    async def get_video_stats(self, bvid: str) -> FetchResult:
        self.video_stats_calls += 1
        return FetchResult(
            request_type=BilibiliRequestType.VIDEO_STATS,
            method="GET",
            url="https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            status_code=200,
            body=json.dumps({"code": 0, "data": {"aid": 777}}).encode(),
            captured_at=datetime(2026, 7, 14, 8, 0, tzinfo=UTC),
        )

    async def get_latest_comments(
        self,
        *,
        aid: int,
        offset: str = "",
    ) -> FetchResult:
        self.latest_offsets.append(offset)
        failures = self.failures.get(offset)
        if failures:
            error = failures.pop(0)
            if self.clock is not None and offset in self.advance_after_offsets:
                self.clock.value = 60
            raise error
        result = FetchResult(
            request_type=BilibiliRequestType.COMMENT_LATEST,
            method="GET",
            url="https://api.bilibili.com/x/v2/reply/wbi/main",
            params={"oid": aid, "offset": offset},
            status_code=200,
            body=self.pages[offset],
            captured_at=datetime(2026, 7, 14, 8, len(self.latest_offsets), tzinfo=UTC),
        )
        if self.clock is not None and offset in self.advance_after_offsets:
            self.clock.value = 60
        return result


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_policy_and_video(session, *, bvid: str, now: datetime) -> None:
    session.add_all(
        [
            CollectionPolicyVersion(
                version="cohort-default-v2",
                policy_kind="snapshot_cohort",
                scope_type="global",
                scope_id="global",
                timezone="Asia/Shanghai",
                policy={"rollout_mode": CohortRolloutMode.SHADOW.value},
                algorithm="configured-fixed-v1",
                created_at=now,
                activated_at=now,
                active=True,
            ),
            KnownVideo(
                bvid=bvid,
                source_mid="42",
                pubdate=now - timedelta(hours=1),
                first_seen_at=now - timedelta(hours=1),
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    await session.flush()


async def _seed_scan_task(
    session,
    *,
    bvid: str,
    now: datetime,
    max_scan_seconds: int = 55,
) -> tuple[CommentScanRun, FrontierState, CollectionTask]:
    await _seed_policy_and_video(session, bvid=bvid, now=now)
    frontier = await FrontierStateRepository(session).get_or_create(
        target_type="video",
        target_id=bvid,
        frontier_type="latest_comments",
        now=now,
    )
    claim = await LatestScanRunRepository(session).claim_or_join(
        LatestScanRunPlan(
            scan_key=f"snapshot:{bvid}:latest:baseline",
            bvid=bvid,
            snapshot_cohort_id=None,
            parent_scan_run_id=None,
            mode=CommentScanMode.BASELINE_TAIL,
            policy_version="cohort-default-v2",
            reason="routine",
            start_frontier_rpid=None,
            start_anchor_set=[],
            start_cursor=None,
            extra={
                "max_scan_seconds": max_scan_seconds,
                "current_head_required": True,
            },
        ),
        frontier_state=frontier,
        expected_version=frontier.version,
        now=now,
    )
    task = await CollectionTaskRepository(session).enqueue(
        kind=TaskKind.FETCH_LATEST_COMMENTS,
        target_type="video",
        target_id=bvid,
        priority=100,
        payload={
            "bvid": bvid,
            "aid": 777,
            "scan_mode": CommentScanMode.BASELINE_TAIL.value,
            "frontier_version": claim.frontier_state.version,
            "max_scan_seconds": max_scan_seconds,
            "current_head_required": True,
        },
        not_before=now,
        idempotency_key=f"{claim.scan.id}:baseline_tail:0",
        comment_scan_run_id=claim.scan.id,
        scan_slice_no=0,
        scan_slice_key=f"{claim.scan.id}:baseline_tail:0",
    )
    return claim.scan, claim.frontier_state, task


def _collector(
    tmp_path,
    client: FakeLatestClient,
    *,
    clock: MutableClock | None = None,
    page_retry_attempts: int = 3,
    page_retry_backoff_seconds: list[float] | None = None,
) -> LatestCommentCollector:
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    return LatestCommentCollector(
        client=client,
        raw_store=RawPayloadFileStore(tmp_path),
        run_id="latest-scan-test",
        max_scan_seconds=55,
        page_retry_attempts=page_retry_attempts,
        page_retry_backoff_seconds=page_retry_backoff_seconds or [0, 0, 0],
        monotonic=clock.monotonic if clock is not None else None,
        sleep=lambda _seconds: None,
        now=lambda: now,
    )


@pytest.mark.asyncio
async def test_legacy_latest_task_keeps_null_scan_evidence(tmp_path) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    client = FakeLatestClient(
        {"": latest_body(rpids=[1001], next_offset="", is_end=True)}
    )

    async with session_factory.begin() as session:
        await _seed_policy_and_video(session, bvid="BV-LEGACY", now=now)
        task = await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type="video",
            target_id="BV-LEGACY",
            priority=70,
            payload={"bvid": "BV-LEGACY", "aid": 777},
            not_before=now,
        )

    async with session_factory.begin() as session:
        task = await session.get(CollectionTask, task.id)
        assert task is not None
        await _collector(tmp_path, client).collect(task, session)

    async with session_factory() as session:
        raw_page = await session.scalar(select(RawPageObservation))
        observation = await session.scalar(select(CommentObservation))
        assert raw_page is not None and raw_page.scan_run_id is None
        assert observation is not None and observation.scan_run_id is None

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["slice_key", "frontier_version"])
async def test_scan_task_requires_identity_before_network(
    tmp_path,
    missing_field: str,
) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    client = FakeLatestClient(
        {"": latest_body(rpids=[1001], next_offset="", is_end=True)}
    )

    async with session_factory.begin() as session:
        _scan, _frontier, task = await _seed_scan_task(
            session,
            bvid="BV-MISSING-SLICE",
            now=now,
        )
        if missing_field == "slice_key":
            task.scan_slice_key = None
            expected = "slice identity"
        else:
            task.payload = {
                key: value
                for key, value in task.payload.items()
                if key != "frontier_version"
            }
            expected = "frontier_version"
        with pytest.raises(ValueError, match=expected):
            await _collector(tmp_path, client).collect(task, session)

    assert client.latest_offsets == []
    assert client.video_stats_calls == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_baseline_tail_persists_anchors_counters_and_scan_evidence(
    tmp_path,
) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    media_url = "https://i0.hdslb.com/bfs/new_dyn/scan-image.jpg"
    client = FakeLatestClient(
        {
            "": latest_body(
                rpids=[1106, 1105, 1104, 1103, 1102, 1101],
                next_offset="offset-2",
                media_url=media_url,
            ),
            "offset-2": latest_body(
                rpids=[1100],
                next_offset="",
                is_end=True,
            ),
        }
    )

    async with session_factory.begin() as session:
        scan, _frontier, task = await _seed_scan_task(
            session,
            bvid="BV-BASELINE-EVIDENCE",
            now=now,
        )
        scan_id = scan.id
        task_id = task.id

    async with session_factory.begin() as session:
        task = await session.get(CollectionTask, task_id)
        assert task is not None
        draft = await _collector(tmp_path, client).collect(task, session)
        assert draft.pages_requested == 2
        assert draft.pages_succeeded == 2
        assert draft.items_observed == 7
        assert draft.raw_payloads_saved == 2
        assert draft.reason == "tail_reached"

    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        frontier = await session.scalar(select(FrontierState))
        raw_pages = list(
            await session.scalars(
                select(RawPageObservation).order_by(RawPageObservation.id)
            )
        )
        observations = list(
            await session.scalars(
                select(CommentObservation).order_by(CommentObservation.id)
            )
        )
        media_link = await session.scalar(select(CommentObservationMedia))

        assert scan is not None
        assert frontier is not None
        assert scan.status is CommentScanStatus.COMPLETE
        assert scan.outcome == "tail_reached"
        assert scan.start_frontier_rpid == 1106
        assert [item["rpid"] for item in scan.start_anchor_set] == [
            1106,
            1105,
            1104,
            1103,
            1102,
        ]
        assert scan.pages_requested == 2
        assert scan.pages_succeeded == 2
        assert scan.items_observed == 7
        assert scan.raw_payloads_saved == 2
        assert scan.slice_count == 1
        assert frontier.active_scan_run_id is None
        assert frontier.cursor == ""
        assert frontier.last_scan_status == "baseline_tail_complete"
        assert frontier.extra["baseline_status"] == "baseline_tail_complete"
        assert all(page.scan_run_id == scan_id for page in raw_pages)
        assert all(row.scan_run_id == scan_id for row in observations)
        assert media_link is not None
        assert media_link.rpid == 1106

    await engine.dispose()


@pytest.mark.asyncio
async def test_baseline_tail_yields_and_resumes_from_saved_cursor(tmp_path) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    clock = MutableClock()
    client = FakeLatestClient(
        {
            "": latest_body(rpids=[1202], next_offset="offset-2"),
            "offset-2": latest_body(
                rpids=[1201],
                next_offset="",
                is_end=True,
            ),
        },
        clock=clock,
        advance_after_offsets={""},
    )
    collector = _collector(tmp_path, client, clock=clock)

    async with session_factory.begin() as session:
        scan, _frontier, task = await _seed_scan_task(
            session,
            bvid="BV-BASELINE-RESUME",
            now=now,
        )
        scan_id = scan.id
        task_id = task.id

    async with session_factory.begin() as session:
        task = await session.get(CollectionTask, task_id)
        assert task is not None
        draft = await collector.collect(task, session)
        assert draft.truncated is True
        assert draft.reason == "time_slice_yield"

    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        frontier = await session.scalar(select(FrontierState))
        tasks = list(
            await session.scalars(select(CollectionTask).order_by(CollectionTask.id))
        )
        assert scan is not None and scan.status is CommentScanStatus.PAUSED
        assert frontier is not None and frontier.cursor == "offset-2"
        assert len(tasks) == 2
        followup = tasks[1]
        assert followup.comment_scan_run_id == scan_id
        assert followup.scan_slice_no == 1
        assert followup.scan_slice_key == f"{scan_id}:baseline_tail:1"
        assert followup.payload["frontier_version"] == frontier.version
        followup_id = followup.id

    clock.value = 0
    client.advance_after_offsets.clear()
    async with session_factory.begin() as session:
        followup = await session.get(CollectionTask, followup_id)
        assert followup is not None
        draft = await collector.collect(followup, session)
        assert draft.reason == "tail_reached"

    assert client.latest_offsets == ["", "offset-2"]
    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        assert scan is not None
        assert scan.status is CommentScanStatus.COMPLETE
        assert scan.pages_requested == 2
        assert scan.pages_succeeded == 2
        assert scan.slice_count == 2

    await engine.dispose()


@pytest.mark.asyncio
async def test_cursor_retry_count_persists_across_slices(tmp_path) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    client = FakeLatestClient(
        {"": latest_body(rpids=[1301], next_offset="", is_end=True)},
        failures={"": [RuntimeError("temporary failure")]},
    )
    collector = _collector(
        tmp_path,
        client,
        page_retry_attempts=3,
        page_retry_backoff_seconds=[60, 60, 60],
    )

    async with session_factory.begin() as session:
        scan, _frontier, task = await _seed_scan_task(
            session,
            bvid="BV-RETRY-RESUME",
            now=now,
        )
        scan_id = scan.id
        task_id = task.id

    async with session_factory.begin() as session:
        task = await session.get(CollectionTask, task_id)
        assert task is not None
        draft = await collector.collect(task, session)
        assert draft.reason == "time_slice_yield"

    async with session_factory() as session:
        frontier = await session.scalar(select(FrontierState))
        followup = await session.scalar(
            select(CollectionTask)
            .where(CollectionTask.scan_slice_no == 1)
            .order_by(CollectionTask.id)
        )
        assert frontier is not None
        progress = frontier.extra["latest_scan_progress"]
        assert progress["failed_cursor"] == ""
        assert progress["failed_attempts"] == 1
        assert followup is not None
        followup_id = followup.id

    async with session_factory.begin() as session:
        followup = await session.get(CollectionTask, followup_id)
        assert followup is not None
        draft = await collector.collect(followup, session)
        assert draft.reason == "tail_reached"

    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        assert scan is not None
        assert scan.pages_requested == 2
        assert scan.pages_succeeded == 1
        assert scan.status is CommentScanStatus.COMPLETE

    await engine.dispose()


@pytest.mark.asyncio
async def test_cursor_retry_exhaustion_counts_across_slices(tmp_path) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    client = FakeLatestClient(
        {"": latest_body(rpids=[1351], next_offset="", is_end=True)},
        failures={
            "": [
                RuntimeError("failure one"),
                RuntimeError("failure two"),
                RuntimeError("failure three"),
            ]
        },
    )
    first_collector = _collector(
        tmp_path,
        client,
        page_retry_attempts=3,
        page_retry_backoff_seconds=[60, 0, 0],
    )

    async with session_factory.begin() as session:
        scan, _frontier, task = await _seed_scan_task(
            session,
            bvid="BV-RETRY-EXHAUSTED",
            now=now,
        )
        scan_id = scan.id
        task_id = task.id

    async with session_factory.begin() as session:
        task = await session.get(CollectionTask, task_id)
        assert task is not None
        first = await first_collector.collect(task, session)
        assert first.reason == "time_slice_yield"

    async with session_factory() as session:
        followup = await session.scalar(
            select(CollectionTask).where(CollectionTask.scan_slice_no == 1)
        )
        assert followup is not None
        followup_id = followup.id

    second_collector = _collector(
        tmp_path,
        client,
        page_retry_attempts=3,
        page_retry_backoff_seconds=[0, 0, 0],
    )
    async with session_factory.begin() as session:
        followup = await session.get(CollectionTask, followup_id)
        assert followup is not None
        second = await second_collector.collect(followup, session)
        assert second.corrupted is True
        assert second.reason == "retry_exhausted"

    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        frontier = await session.scalar(select(FrontierState))
        tasks = list(await session.scalars(select(CollectionTask)))
        assert scan is not None
        assert scan.pages_requested == 3
        assert scan.pages_succeeded == 0
        assert scan.status is CommentScanStatus.CORRUPTED
        assert scan.outcome == "retry_exhausted"
        assert frontier is not None
        assert frontier.active_scan_run_id is None
        assert frontier.extra["latest_scan_progress"]["failed_attempts"] == 3
        assert len(tasks) == 2

    await engine.dispose()


@pytest.mark.asyncio
async def test_repeated_cursor_corrupts_scan_without_followup(tmp_path) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    client = FakeLatestClient(
        {
            "": latest_body(rpids=[1402], next_offset="offset-2"),
            "offset-2": latest_body(rpids=[1401], next_offset="offset-2"),
        }
    )

    async with session_factory.begin() as session:
        scan, _frontier, task = await _seed_scan_task(
            session,
            bvid="BV-CURSOR-LOOP",
            now=now,
        )
        scan_id = scan.id
        task_id = task.id

    async with session_factory.begin() as session:
        task = await session.get(CollectionTask, task_id)
        assert task is not None
        draft = await _collector(tmp_path, client).collect(task, session)
        assert draft.corrupted is True
        assert draft.reason == "cursor_loop"

    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        frontier = await session.scalar(select(FrontierState))
        assert scan is not None
        assert scan.status is CommentScanStatus.CORRUPTED
        assert scan.outcome == "cursor_loop"
        assert frontier is not None and frontier.active_scan_run_id is None
        assert await session.scalar(select(func.count(CollectionTask.id))) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_frontier_version_fails_before_network_or_progress(
    tmp_path,
) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    client = FakeLatestClient(
        {"": latest_body(rpids=[1501], next_offset="", is_end=True)}
    )

    async with session_factory.begin() as session:
        scan, frontier, task = await _seed_scan_task(
            session,
            bvid="BV-STALE-FRONTIER",
            now=now,
        )
        scan_id = scan.id
        task_id = task.id
        await FrontierStateRepository(session).compare_and_swap(
            frontier.id,
            frontier.version,
            FrontierStateUpdate(
                frontier_rpid=frontier.frontier_rpid,
                frontier_time=frontier.frontier_time,
                frontier_anchor_set=frontier.frontier_anchor_set,
                active_scan_run_id=scan.id,
                cursor=frontier.cursor,
                last_scan_at=frontier.last_scan_at,
                last_scan_status=frontier.last_scan_status,
                last_scan_pages=frontier.last_scan_pages,
                last_scan_truncated=frontier.last_scan_truncated,
                extra=frontier.extra,
            ),
            now=now + timedelta(seconds=1),
        )

    async with session_factory.begin() as session:
        task = await session.get(CollectionTask, task_id)
        assert task is not None
        with pytest.raises(FrontierVersionConflict):
            await _collector(tmp_path, client).collect(task, session)

    assert client.latest_offsets == []
    assert client.video_stats_calls == 0
    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        assert scan is not None
        assert scan.status is CommentScanStatus.PLANNED
        assert scan.pages_requested == 0
        assert scan.pages_succeeded == 0
        assert await session.scalar(select(func.count(RawPageObservation.id))) == 0
        assert await session.scalar(select(func.count(CommentObservation.id))) == 0
        assert await session.scalar(select(func.count(CollectionTask.id))) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_parse_failure_keeps_raw_but_not_page_or_frontier_progress(
    tmp_path,
) -> None:
    engine, session_factory = await _database()
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    client = FakeLatestClient({"": b'{"code": 0, "data": {}}'})

    async with session_factory.begin() as session:
        scan, frontier, task = await _seed_scan_task(
            session,
            bvid="BV-PARSE-RAW",
            now=now,
        )
        scan_id = scan.id
        task_id = task.id
        original_version = frontier.version

    async with session_factory.begin() as session:
        task = await session.get(CollectionTask, task_id)
        assert task is not None
        with pytest.raises(Exception, match="cursor"):
            await _collector(tmp_path, client).collect(task, session)

    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        frontier = await session.scalar(select(FrontierState))
        assert scan is not None
        assert frontier is not None
        assert await session.scalar(select(func.count(RawPayload.id))) == 1
        assert await session.scalar(select(func.count(RawPageObservation.id))) == 0
        assert await session.scalar(select(func.count(CommentObservation.id))) == 0
        assert scan.pages_requested == 1
        assert scan.pages_succeeded == 0
        assert frontier.version == original_version
        assert frontier.cursor is None

    await engine.dispose()
