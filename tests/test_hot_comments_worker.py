import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.collectors.hot_comments import HotCommentCollector
from books_of_time.db.comment_scan_repositories import (
    CommentScanRunRepository,
    HotScanRunPlan,
)
from books_of_time.db.models import (
    Base,
    CollectionCoverageStat,
    CollectionTask,
    CommentEntity,
    CommentObservation,
    CommentObservationMedia,
    CommentScanRun,
    ImportantCommentWatchlist,
    MediaSource,
    RawPageObservation,
    RawPayload,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import (
    BilibiliRequestType,
    CommentScanMode,
    CommentScanStatus,
    TaskKind,
    TaskStatus,
)
from books_of_time.http.client import FetchResult
from books_of_time.http.errors import RequestErrorKind, RequestFailure
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


class FakeScanHotCommentsClient:
    def __init__(
        self,
        *,
        end_pages: set[int] | None = None,
        empty_pages: set[int] | None = None,
        repeated_rpid: int | None = None,
        fail_page: int | None = None,
    ) -> None:
        self.pages: list[int] = []
        self.end_pages = end_pages or set()
        self.empty_pages = empty_pages or set()
        self.repeated_rpid = repeated_rpid
        self.fail_page = fail_page

    async def get_video_stats(self, bvid: str) -> FetchResult:
        raise AssertionError("aid is supplied in task payload")

    async def get_hot_comments(self, *, aid: int, page: int = 1) -> FetchResult:
        self.pages.append(page)
        if page == self.fail_page:
            raise RequestFailure(
                kind=RequestErrorKind.NETWORK,
                request_type=BilibiliRequestType.COMMENT_HOT,
                message=f"page {page} failed",
            )
        replies = []
        if page not in self.empty_pages:
            rpid = self.repeated_rpid or 1000 + page
            replies = [
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
            ]
        cursor = {"all_count": 999}
        if page in self.end_pages:
            cursor["is_end"] = True
        body = json.dumps(
            {
                "code": 0,
                "data": {"cursor": cursor, "replies": replies},
            }
        ).encode()
        return FetchResult(
            request_type=BilibiliRequestType.COMMENT_HOT,
            method="GET",
            url="https://api.bilibili.com/x/v2/reply",
            params={"oid": aid, "pn": page, "sort": 2},
            status_code=200,
            body=body,
            captured_at=datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
            + timedelta(minutes=page),
        )


class SequenceClock:
    def __init__(self, values: list[float]) -> None:
        self.values = iter(values)
        self.last = values[-1]

    def __call__(self) -> float:
        try:
            self.last = next(self.values)
        except StopIteration:
            pass
        return self.last


async def _enqueue_scan_task(
    session,
    *,
    now: datetime,
    mode: CommentScanMode,
    start_page: int,
    end_page: int,
    max_pages_per_slice: int = 10,
    max_scan_seconds: float = 55,
) -> tuple[CommentScanRun, CollectionTask]:
    target_pages = end_page - start_page + 1
    scan, _ = await CommentScanRunRepository(session).materialize_hot(
        HotScanRunPlan(
            scan_key=f"snapshot:BV-SCAN:{mode.value}",
            bvid="BV-SCAN",
            snapshot_cohort_id=None,
            mode=mode,
            target_pages=target_pages,
            start_page=start_page,
            end_page=end_page,
            policy_version="cohort-default-v2",
            extra={
                "max_pages_per_slice": max_pages_per_slice,
                "max_scan_seconds": max_scan_seconds,
            },
        ),
        now=now,
    )
    task = await CollectionTaskRepository(session).enqueue(
        kind=TaskKind.FETCH_HOT_COMMENTS,
        target_type="video",
        target_id="BV-SCAN",
        priority=121,
        budget_cost=2,
        payload={
            "bvid": "BV-SCAN",
            "aid": 777,
            "scan_mode": mode.value,
            "start_page": start_page,
            "end_page": end_page,
            "target_pages": target_pages,
            "max_pages_per_slice": max_pages_per_slice,
            "max_scan_seconds": max_scan_seconds,
            "page": start_page,
            "page_limit": target_pages,
        },
        not_before=now,
        max_retries=3,
        idempotency_key=f"{scan.scan_key}:{mode.value}:active:0",
        comment_scan_run_id=scan.id,
        scan_slice_no=0,
        scan_slice_key=f"{scan.id}:{mode.value}:0",
    )
    return scan, task


def _scan_worker(
    *,
    session_factory,
    client: FakeScanHotCommentsClient,
    tmp_path,
    now: datetime,
    monotonic,
) -> Worker:
    return Worker(
        session_factory=session_factory,
        collectors={
            TaskKind.FETCH_HOT_COMMENTS: HotCommentCollector(
                client=client,
                raw_store=RawPayloadFileStore(tmp_path),
                run_id="scan-test-run",
                monotonic=monotonic,
                now=lambda: now,
            )
        },
        run_id="scan-test-run",
        lease_owner="scan-worker",
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
        assert len(tasks) == 3
        media_task = next(
            task for task in tasks if task.kind == TaskKind.FETCH_MEDIA_ASSET
        )
        reply_task = next(
            task for task in tasks if task.kind == TaskKind.FETCH_COMMENT_REPLIES
        )
        assert media_task.target_type == "media_source"
        assert media_task.payload["url"] == "https://i0.hdslb.com/bfs/new_dyn/a.jpg"
        assert reply_task.target_type == "comment"
        assert reply_task.target_id == "1001"
        assert reply_task.payload["root_rpid"] == 1001
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
        assert watch.expires_at is not None
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
        assert {page.scan_run_id for page in raw_pages} == {None}
        assert [observation.rpid for observation in observations] == [1001, 1002]
        assert {observation.scan_run_id for observation in observations} == {None}
        assert len(raw_payloads) == 2

    await engine.dispose()


@pytest.mark.asyncio
async def test_hot_deep_scan_runs_in_numbered_ten_page_slices(tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        scan, first_task = await _enqueue_scan_task(
            session,
            now=now,
            mode=CommentScanMode.HOT_DEEP,
            start_page=4,
            end_page=20,
        )
        scan_id = scan.id
        first_task_id = first_task.id
        await session.commit()

    client = FakeScanHotCommentsClient()
    worker = _scan_worker(
        session_factory=session_factory,
        client=client,
        tmp_path=tmp_path,
        now=now,
        monotonic=lambda: 0.0,
    )
    assert await worker.run_once(now=now) is True

    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        tasks = list(
            await session.scalars(select(CollectionTask).order_by(CollectionTask.id))
        )
        scan_tasks = [
            task
            for task in tasks
            if task.kind is TaskKind.FETCH_HOT_COMMENTS
            and task.comment_scan_run_id == scan_id
        ]
        coverage = await session.scalar(
            select(CollectionCoverageStat).where(
                CollectionCoverageStat.collection_task_id == first_task_id
            )
        )
        raw_pages = list(await session.scalars(select(RawPageObservation)))
        observations = list(await session.scalars(select(CommentObservation)))

        assert client.pages == list(range(4, 14))
        assert scan is not None
        assert scan.status is CommentScanStatus.PAUSED
        assert scan.outcome == "time_slice_yield"
        assert scan.next_page_number == 14
        assert scan.pages_requested == 10
        assert scan.pages_succeeded == 10
        assert scan.items_observed == 10
        assert scan.raw_payloads_saved == 10
        assert scan.slice_count == 1
        assert len(scan_tasks) == 2
        assert scan_tasks[0].status is TaskStatus.SUCCEEDED
        assert scan_tasks[1].status is TaskStatus.PENDING
        assert scan_tasks[1].scan_slice_no == 1
        assert scan_tasks[1].scan_slice_key == f"{scan_id}:hot_deep:1"
        assert scan_tasks[1].payload["page"] == 14
        assert scan_tasks[1].priority == scan_tasks[0].priority
        assert scan_tasks[1].budget_cost == scan_tasks[0].budget_cost
        assert scan_tasks[1].max_retries == scan_tasks[0].max_retries
        assert coverage is not None
        assert coverage.status == "partial"
        assert coverage.reason == "time_slice_yield"
        assert coverage.truncated is True
        assert coverage.pages_requested == 10
        assert coverage.pages_succeeded == 10
        assert {page.scan_run_id for page in raw_pages} == {scan_id}
        assert {observation.scan_run_id for observation in observations} == {scan_id}

    assert await worker.run_once(now=now + timedelta(seconds=1)) is True

    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        tasks = list(
            await session.scalars(select(CollectionTask).order_by(CollectionTask.id))
        )
        scan_tasks = [
            task
            for task in tasks
            if task.kind is TaskKind.FETCH_HOT_COMMENTS
            and task.comment_scan_run_id == scan_id
        ]
        coverages = list(
            await session.scalars(
                select(CollectionCoverageStat).order_by(CollectionCoverageStat.id)
            )
        )

        assert client.pages == list(range(4, 21))
        assert scan is not None
        assert scan.status is CommentScanStatus.COMPLETE
        assert scan.outcome == "target_reached"
        assert scan.next_page_number == 21
        assert scan.pages_requested == 17
        assert scan.pages_succeeded == 17
        assert scan.items_observed == 17
        assert scan.raw_payloads_saved == 17
        assert scan.slice_count == 2
        assert len(scan_tasks) == 2
        assert all(task.status is TaskStatus.SUCCEEDED for task in scan_tasks)
        assert coverages[1].status == "succeeded"
        assert coverages[1].reason == "target_reached"
        assert coverages[1].pages_requested == 7
        assert coverages[1].pages_succeeded == 7

    await engine.dispose()


@pytest.mark.asyncio
async def test_hot_scan_yields_when_55_second_budget_is_crossed(tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        scan, _ = await _enqueue_scan_task(
            session,
            now=now,
            mode=CommentScanMode.HOT_CORE,
            start_page=1,
            end_page=5,
        )
        scan_id = scan.id
        await session.commit()

    client = FakeScanHotCommentsClient()
    worker = _scan_worker(
        session_factory=session_factory,
        client=client,
        tmp_path=tmp_path,
        now=now,
        monotonic=SequenceClock([0.0, 56.0]),
    )
    await worker.run_once(now=now)

    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        follow_up = await session.scalar(
            select(CollectionTask).where(CollectionTask.scan_slice_no == 1)
        )
        assert client.pages == [1]
        assert scan is not None and scan.status is CommentScanStatus.PAUSED
        assert scan.next_page_number == 2
        assert follow_up is not None
        assert follow_up.payload["page"] == 2

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "expected_items"),
    [
        (FakeScanHotCommentsClient(end_pages={1}), 1),
        (FakeScanHotCommentsClient(empty_pages={1}), 0),
    ],
)
async def test_hot_scan_server_end_completes_without_follow_up(
    tmp_path,
    client: FakeScanHotCommentsClient,
    expected_items: int,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        scan, _ = await _enqueue_scan_task(
            session,
            now=now,
            mode=CommentScanMode.HOT_CORE,
            start_page=1,
            end_page=10,
        )
        scan_id = scan.id
        await session.commit()

    worker = _scan_worker(
        session_factory=session_factory,
        client=client,
        tmp_path=tmp_path,
        now=now,
        monotonic=lambda: 0.0,
    )
    await worker.run_once(now=now)

    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        tasks = list(await session.scalars(select(CollectionTask)))
        scan_tasks = [
            task
            for task in tasks
            if task.kind is TaskKind.FETCH_HOT_COMMENTS
            and task.comment_scan_run_id == scan_id
        ]
        assert client.pages == [1]
        assert scan is not None and scan.status is CommentScanStatus.COMPLETE
        assert scan.outcome == "server_end"
        assert scan.pages_succeeded == 1
        assert scan.items_observed == expected_items
        assert len(scan_tasks) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_hot_scan_repeated_rpid_keeps_direct_page_and_comment_evidence(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        scan, _ = await _enqueue_scan_task(
            session,
            now=now,
            mode=CommentScanMode.HOT_CORE,
            start_page=1,
            end_page=2,
        )
        scan_id = scan.id
        await session.commit()

    client = FakeScanHotCommentsClient(repeated_rpid=1001)
    await _scan_worker(
        session_factory=session_factory,
        client=client,
        tmp_path=tmp_path,
        now=now,
        monotonic=lambda: 0.0,
    ).run_once(now=now)

    async with session_factory() as session:
        raw_pages = list(
            await session.scalars(
                select(RawPageObservation).order_by(RawPageObservation.page_number)
            )
        )
        observations = list(
            await session.scalars(
                select(CommentObservation).order_by(CommentObservation.id)
            )
        )
        entities = list(await session.scalars(select(CommentEntity)))

        assert [page.page_number for page in raw_pages] == [1, 2]
        assert [page.scan_run_id for page in raw_pages] == [scan_id, scan_id]
        assert [observation.rpid for observation in observations] == [1001, 1001]
        assert [observation.scan_run_id for observation in observations] == [
            scan_id,
            scan_id,
        ]
        assert len(entities) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_hot_scan_request_failure_keeps_successful_page_progress(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    async with session_factory() as session:
        scan, task = await _enqueue_scan_task(
            session,
            now=now,
            mode=CommentScanMode.HOT_CORE,
            start_page=1,
            end_page=3,
        )
        scan_id = scan.id
        task_id = task.id
        await session.commit()

    client = FakeScanHotCommentsClient(fail_page=2)
    await _scan_worker(
        session_factory=session_factory,
        client=client,
        tmp_path=tmp_path,
        now=now,
        monotonic=lambda: 0.0,
    ).run_once(now=now)

    async with session_factory() as session:
        scan = await session.get(CommentScanRun, scan_id)
        task = await session.get(CollectionTask, task_id)
        coverage = await session.scalar(select(CollectionCoverageStat))
        raw_pages = list(await session.scalars(select(RawPageObservation)))

        assert client.pages == [1, 2]
        assert scan is not None and scan.status is CommentScanStatus.RUNNING
        assert scan.next_page_number == 2
        assert scan.pages_requested == 2
        assert scan.pages_succeeded == 1
        assert scan.items_observed == 1
        assert scan.raw_payloads_saved == 1
        assert scan.last_error_type == "RequestFailure"
        assert scan.last_error_message == "page 2 failed"
        assert task is not None and task.status is TaskStatus.PENDING
        assert task.retry_count == 1
        assert coverage is not None and coverage.status == "failed"
        assert [page.page_number for page in raw_pages] == [1]
        assert raw_pages[0].scan_run_id == scan_id

    await engine.dispose()
