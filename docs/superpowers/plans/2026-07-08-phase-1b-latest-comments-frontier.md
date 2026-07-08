# Phase 1B Latest Comments Frontier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build resumable Bilibili latest-comment collection with baseline tail scan, baseline head sweep, incremental frontier scans, raw evidence, and explicit paused/corrupted coverage state.

**Architecture:** Keep the existing collector architecture: platform clients only call and capture Bilibili requests, parsers normalize response JSON into dataclasses, repositories own ORM persistence, and collectors orchestrate one task inside the worker transaction. Phase 1B extends the existing comment data foundation by adding cursor-aware latest pages and a lightweight `frontier_states.extra` JSON state machine instead of adding collection-run tables.

**Tech Stack:** Python 3.12, uv, pytest + pytest-asyncio, Ruff, SQLAlchemy asyncio ORM, SQLite in tests, PostgreSQL-compatible ORM models, bilibili-api-python, curl-cffi raw HTTP backend, zstandard raw payload storage.

## Global Constraints

- `page_limit` must not be used to mean "total comments per request"; the Bilibili lazy comments API does not expose a page-size parameter.
- A single collector run must keep outbound requests within a one-minute update window.
- The default request time slice is 55 seconds.
- Page-level request failure is retried with configurable attempts and backoff.
- If the same page/cursor fails after all configured attempts, the scan stops and is marked `corrupted`, not `complete`.
- If the time slice expires without a failed page, the scan is marked `paused` and can resume from the saved cursor.
- Retry attempts and retry sleeps are also bounded by `max_scan_seconds`.
- If a page has failed but the current run reaches the time slice before exhausting configured attempts, record `failed_cursor`, `failed_reason`, and `failed_attempts`, then resume retrying that same cursor in a later run.
- Comment authors remain non-anonymized. Store public `mid` and display name when present.
- Store readable comment content and `content_hash`; the hash is only a comparison aid.
- The first baseline is an observation window, not an atomic t0 snapshot.
- `frontier_states.extra` is the only Phase 1B baseline-state extension column.
- Do not add an ORM attribute named `metadata`.
- Final verification commands are `uv run pytest` and `uv run ruff check .`.

---

## File Structure

- Modify `books_of_time/parsers/comments.py`: add `parse_latest_comment_page`, cursor extraction, and latest-page end detection.
- Modify `tests/test_comments_parser.py`: add latest parser tests for normal page, empty end page, and malformed cursor.
- Modify `books_of_time/platforms/bilibili/client.py`: add `get_latest_comments(aid, offset="")`.
- Modify `tests/test_bilibili_client.py`: add lazy comments API fake and latest request capture test.
- Modify `config/config.yaml.example`: add `latest_comments` defaults.
- Modify `books_of_time/db/models.py`: add `FrontierState.extra`.
- Modify `books_of_time/db/repositories.py`: add `FrontierStateRepository` and persist latest-page cursor on `RawPageObservation`.
- Modify `tests/test_comment_repositories.py`: add frontier repository and latest raw page tests.
- Create `books_of_time/collectors/latest_comments.py`: implement latest-comment state machine, retry loop, baseline tail, head sweep, and incremental scans.
- Create `tests/test_latest_comments_worker.py`: integration tests for pause/resume, corruption, head sweep, incremental success, and missing frontier.
- Modify `books_of_time/app.py`: register `TaskKind.FETCH_LATEST_COMMENTS` with config-driven collector options.
- Modify `books_of_time/cli.py`: add `bot collect-latest-comments BVxxxx`.
- Create `tests/test_cli.py`: parser test for latest-comments enqueue command.
- Modify `docs/TODO.md`: check the completed latest-comments Phase 1B items after verification.

---

### Task 1: Latest Comment Parser, Platform Method, And Config Defaults

**Files:**
- Modify: `books_of_time/parsers/comments.py`
- Modify: `tests/test_comments_parser.py`
- Modify: `books_of_time/platforms/bilibili/client.py`
- Modify: `tests/test_bilibili_client.py`
- Modify: `config/config.yaml.example`

**Interfaces:**
- Consumes: `bilibili_api.comment.get_comments_lazy(oid, type_, offset="", order=comment.OrderType.TIME)`.
- Produces:
  - `parse_latest_comment_page(payload: dict[str, Any], *, bvid: str, oid: int, captured_at: datetime, raw_payload_id: int, page_number: int, request_offset: str) -> ParsedCommentPage`
  - latest `ParsedCommentPage.sort_mode == "latest"`
  - latest `ParsedCommentPage.extra["request_offset"]`
  - latest `ParsedCommentPage.extra["next_offset"]`
  - latest `ParsedCommentPage.extra["is_end"]`
  - `BilibiliPlatformClient.get_latest_comments(self, *, aid: int, offset: str = "") -> FetchResult`
  - config keys under `latest_comments`: `max_scan_seconds`, `page_retry_attempts`, `page_retry_backoff_seconds`

- [ ] **Step 1: Add latest parser tests**

Append to `tests/test_comments_parser.py`:

```python
def latest_payload(
    *,
    replies: list[dict] | None,
    next_offset: str = "offset-2",
    is_end: bool = False,
) -> dict:
    return {
        "code": 0,
        "data": {
            "cursor": {
                "pagination_reply": {"next_offset": next_offset},
                "is_end": is_end,
            },
            "replies": replies,
        },
    }


def test_parse_latest_comment_page_extracts_cursor_and_comments() -> None:
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    page = parse_latest_comment_page(
        latest_payload(
            replies=[
                {
                    "rpid": 2001,
                    "oid": 777,
                    "root": 0,
                    "parent": 0,
                    "like": 3,
                    "rcount": 0,
                    "ctime": 1783490000,
                    "member": {"mid": "42", "uname": "Alice"},
                    "content": {"message": "latest comment"},
                }
            ],
            next_offset="offset-2",
        ),
        bvid="BV1abc",
        oid=777,
        captured_at=captured_at,
        raw_payload_id=42,
        page_number=1,
        request_offset="",
    )

    assert page.sort_mode == "latest"
    assert page.page_number == 1
    assert page.extra["request_offset"] == ""
    assert page.extra["next_offset"] == "offset-2"
    assert page.extra["is_end"] is False
    assert page.comments[0].rpid == 2001
    assert page.comments[0].author_mid == 42
    assert page.comments[0].author_name == "Alice"
    assert page.comments[0].content == "latest comment"


def test_parse_latest_comment_page_accepts_empty_end_page() -> None:
    page = parse_latest_comment_page(
        latest_payload(replies=None, next_offset="", is_end=True),
        bvid="BV1abc",
        oid=777,
        captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        raw_payload_id=42,
        page_number=2,
        request_offset="offset-2",
    )

    assert page.comments == []
    assert page.extra["request_offset"] == "offset-2"
    assert page.extra["next_offset"] == ""
    assert page.extra["is_end"] is True


def test_parse_latest_comment_page_rejects_malformed_cursor() -> None:
    with pytest.raises(CommentParseError, match="pagination_reply"):
        parse_latest_comment_page(
            {"code": 0, "data": {"cursor": {}, "replies": []}},
            bvid="BV1abc",
            oid=777,
            captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            raw_payload_id=42,
            page_number=1,
            request_offset="",
        )
```

Update the existing import block in `tests/test_comments_parser.py`:

```python
from books_of_time.parsers.comments import (
    CommentParseError,
    hash_comment_content,
    parse_hot_comment_page,
    parse_latest_comment_page,
)
```

- [ ] **Step 2: Run latest parser tests and verify failure**

Run:

```bash
uv run pytest tests/test_comments_parser.py::test_parse_latest_comment_page_extracts_cursor_and_comments tests/test_comments_parser.py::test_parse_latest_comment_page_accepts_empty_end_page tests/test_comments_parser.py::test_parse_latest_comment_page_rejects_malformed_cursor -v
```

Expected: fails with `ImportError` or `NameError` for `parse_latest_comment_page`.

- [ ] **Step 3: Implement latest parser**

Add to `books_of_time/parsers/comments.py` after `parse_hot_comment_page`:

```python
def parse_latest_comment_page(
    payload: dict[str, Any],
    *,
    bvid: str,
    oid: int,
    captured_at: datetime,
    raw_payload_id: int,
    page_number: int,
    request_offset: str,
) -> ParsedCommentPage:
    code = payload.get("code")
    if code not in (0, None):
        raise CommentParseError(f"Bilibili comment response code is not 0: {code}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise CommentParseError("Bilibili comment response data is not an object")

    cursor = data.get("cursor")
    if not isinstance(cursor, dict):
        raise CommentParseError("Bilibili latest comment cursor is not an object")

    pagination_reply = cursor.get("pagination_reply")
    if not isinstance(pagination_reply, dict):
        raise CommentParseError(
            "Bilibili latest comment cursor.pagination_reply is not an object"
        )

    next_offset = pagination_reply.get("next_offset")
    if next_offset is None:
        next_offset = ""
    if not isinstance(next_offset, str):
        raise CommentParseError(
            "Bilibili latest comment cursor.pagination_reply.next_offset is not a string"
        )

    replies = data.get("replies")
    if replies is None:
        replies = []
    if not isinstance(replies, list):
        raise CommentParseError("Bilibili comment response data.replies is not a list")

    comments = [
        _parse_comment(
            item,
            bvid=bvid,
            fallback_oid=oid,
            position=index,
        )
        for index, item in enumerate(replies, start=1)
        if isinstance(item, dict)
    ]
    is_end = bool(cursor.get("is_end")) or next_offset == "" or len(comments) == 0
    return ParsedCommentPage(
        bvid=bvid,
        oid=oid,
        captured_at=captured_at,
        raw_payload_id=raw_payload_id,
        sort_mode="latest",
        page_number=page_number,
        comments=comments,
        extra={
            "request_offset": request_offset,
            "next_offset": next_offset,
            "is_end": is_end,
        },
    )
```

- [ ] **Step 4: Add latest platform client test**

Append to `tests/test_bilibili_client.py`:

```python
class FakeLatestCommentOrderType:
    LIKE = type("LikeOrder", (), {"value": 2})()
    TIME = type("TimeOrder", (), {"value": 3})()


async def fake_get_comments_lazy(oid, type_, offset, order):
    from bilibili_api.utils.network import get_client

    response = await get_client().request(
        method="GET",
        url="https://api.bilibili.com/x/v2/reply/wbi/main",
        params={
            "oid": oid,
            "type": type_.value,
            "mode": 2,
            "pagination_str": offset,
            "plat": 1,
            "seek_rpid": "",
            "web_location": 1315875,
        },
        headers={},
        cookies={},
    )
    return response.json()["data"]


@pytest.mark.asyncio
async def test_latest_comments_uses_lazy_bilibili_api_client_backend(monkeypatch) -> None:
    raw_http_client = FakeRawHttpClient()
    rate_limiter = FakeRateLimiter()
    monkeypatch.setattr(
        "bilibili_api.comment.CommentResourceType",
        FakeCommentResourceType,
    )
    monkeypatch.setattr(
        "bilibili_api.comment.OrderType",
        FakeLatestCommentOrderType,
    )
    monkeypatch.setattr(
        "bilibili_api.comment.get_comments_lazy",
        fake_get_comments_lazy,
    )

    client = BilibiliPlatformClient(
        http_client=raw_http_client,
        rate_limiter=rate_limiter,
    )

    result = await client.get_latest_comments(aid=777, offset="offset-2")

    assert result.request_type == BilibiliRequestType.COMMENT_LATEST
    assert raw_http_client.requests[0]["url"].endswith("/x/v2/reply/wbi/main")
    assert raw_http_client.requests[0]["params"]["oid"] == 777
    assert raw_http_client.requests[0]["params"]["mode"] == 2
    assert raw_http_client.requests[0]["params"]["pagination_str"] == "offset-2"
    assert rate_limiter.keys == [
        "global",
        "host:bilibili",
        "bilibili:comment_latest",
    ]
```

- [ ] **Step 5: Run platform test and verify failure**

Run:

```bash
uv run pytest tests/test_bilibili_client.py::test_latest_comments_uses_lazy_bilibili_api_client_backend -v
```

Expected: fails with `AttributeError: 'BilibiliPlatformClient' object has no attribute 'get_latest_comments'`.

- [ ] **Step 6: Implement latest platform method**

Add inside `BilibiliPlatformClient` in `books_of_time/platforms/bilibili/client.py`:

```python
    async def get_latest_comments(self, *, aid: int, offset: str = "") -> FetchResult:
        with capture_bili_api_requests(
            http_client=self.http_client,
            rate_limiter=self.rate_limiter,
        ) as request_context:
            await comment.get_comments_lazy(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                offset=offset,
                order=comment.OrderType.TIME,
            )
            return request_context.latest_result(BilibiliRequestType.COMMENT_LATEST)
```

- [ ] **Step 7: Add config defaults**

Append to `config/config.yaml.example` after the `scheduler` block:

```yaml
latest_comments:
  max_scan_seconds: 55
  page_retry_attempts: 3
  page_retry_backoff_seconds: [1, 3, 5]
```

- [ ] **Step 8: Run task verification**

Run:

```bash
uv run pytest tests/test_comments_parser.py tests/test_bilibili_client.py tests/test_config_loader.py -v
uv run ruff check books_of_time/parsers/comments.py books_of_time/platforms/bilibili/client.py tests/test_comments_parser.py tests/test_bilibili_client.py
```

Expected: selected tests pass and Ruff reports no issues.

- [ ] **Step 9: Commit task**

Run:

```bash
git add books_of_time/parsers/comments.py tests/test_comments_parser.py books_of_time/platforms/bilibili/client.py tests/test_bilibili_client.py config/config.yaml.example
git commit -m "feat: capture latest comment pages"
```

Expected: commit succeeds. Leave unrelated `README.md` changes unstaged.

---

### Task 2: Frontier State Repository And Cursor Page Persistence

**Files:**
- Modify: `books_of_time/db/models.py`
- Modify: `books_of_time/db/repositories.py`
- Modify: `tests/test_comment_repositories.py`

**Interfaces:**
- Consumes:
  - `ParsedCommentPage.extra["request_offset"]`
  - `ParsedCommentPage.extra["next_offset"]`
  - `FrontierState`
- Produces:
  - `FrontierState.extra: dict[str, Any]`
  - `FrontierStateRepository.get_or_create(target_type: str, target_id: str, frontier_type: str, now: datetime) -> FrontierState`
  - `FrontierStateRepository.save(state: FrontierState) -> FrontierState`
  - `RawPageObservation.cursor == parsed.extra["request_offset"]` for latest pages

- [ ] **Step 1: Add repository tests**

Append to `tests/test_comment_repositories.py`:

```python
from books_of_time.db.models import FrontierState
from books_of_time.db.repositories import FrontierStateRepository


@pytest.mark.asyncio
async def test_frontier_repository_creates_once_and_persists_extra() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = FrontierStateRepository(session)
        state = await repo.get_or_create(
            target_type="video",
            target_id="BV1abc",
            frontier_type="latest_comments",
            now=now,
        )
        state.extra = {
            "baseline_status": "baseline_paused",
            "seen_cursors": [""],
        }
        state.cursor = "offset-2"
        await repo.save(state)
        await session.commit()

    async with session_factory() as session:
        repo = FrontierStateRepository(session)
        same = await repo.get_or_create(
            target_type="video",
            target_id="BV1abc",
            frontier_type="latest_comments",
            now=now,
        )

        assert same.id == state.id
        assert same.cursor == "offset-2"
        assert same.extra["baseline_status"] == "baseline_paused"
        assert same.extra["seen_cursors"] == [""]

    await engine.dispose()


@pytest.mark.asyncio
async def test_latest_raw_page_observation_stores_request_cursor() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    parsed = ParsedCommentPage(
        bvid="BV1abc",
        oid=777,
        captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        raw_payload_id=42,
        sort_mode="latest",
        page_number=3,
        comments=[],
        extra={
            "request_offset": "offset-2",
            "next_offset": "offset-3",
            "is_end": False,
        },
    )

    async with session_factory() as session:
        raw_page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_LATEST,
        )
        await session.commit()

    async with session_factory() as session:
        saved = await session.scalar(select(RawPageObservation))

        assert saved is not None
        assert saved.id == raw_page.id
        assert saved.cursor == "offset-2"
        assert saved.sort_mode == "latest"
        assert saved.extra["next_offset"] == "offset-3"

    await engine.dispose()
```

- [ ] **Step 2: Run repository tests and verify failure**

Run:

```bash
uv run pytest tests/test_comment_repositories.py::test_frontier_repository_creates_once_and_persists_extra tests/test_comment_repositories.py::test_latest_raw_page_observation_stores_request_cursor -v
```

Expected: fails because `FrontierState.extra` and `FrontierStateRepository` are missing, or because `RawPageObservation.cursor` is still `None`.

- [ ] **Step 3: Add `FrontierState.extra`**

Modify `books_of_time/db/models.py` inside `class FrontierState` after `last_scan_truncated`:

```python
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
```

- [ ] **Step 4: Add frontier repository and latest cursor persistence**

Modify the import list in `books_of_time/db/repositories.py`:

```python
from books_of_time.db.models import (
    CollectionTask,
    CommentEntity,
    CommentObservation,
    FrontierState,
    RawPageObservation,
    RawPayload,
    VideoMetricSnapshot,
)
```

Modify `RawPageObservationRepository.insert_from_parsed_page`:

```python
            cursor=parsed.extra.get("request_offset"),
```

Add this repository class after `CommentRepository`:

```python
class FrontierStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(
        self,
        *,
        target_type: str,
        target_id: str,
        frontier_type: str,
        now: datetime,
    ) -> FrontierState:
        stmt = select(FrontierState).where(
            FrontierState.target_type == target_type,
            FrontierState.target_id == target_id,
            FrontierState.frontier_type == frontier_type,
        )
        state = await self.session.scalar(stmt)
        if state is not None:
            return state

        state = FrontierState(
            target_type=target_type,
            target_id=target_id,
            frontier_type=frontier_type,
            frontier_rpid=None,
            frontier_time=None,
            cursor=None,
            last_scan_at=None,
            last_scan_status=None,
            last_scan_pages=0,
            last_scan_truncated=False,
            extra={},
            created_at=now,
            updated_at=now,
        )
        self.session.add(state)
        await self.session.flush()
        return state

    async def save(self, state: FrontierState) -> FrontierState:
        await self.session.flush()
        return state
```

- [ ] **Step 5: Run task verification**

Run:

```bash
uv run pytest tests/test_comment_repositories.py -v
uv run ruff check books_of_time/db/models.py books_of_time/db/repositories.py tests/test_comment_repositories.py
```

Expected: selected tests pass and Ruff reports no issues.

- [ ] **Step 6: Commit task**

Run:

```bash
git add books_of_time/db/models.py books_of_time/db/repositories.py tests/test_comment_repositories.py
git commit -m "feat: store latest comment frontier state"
```

Expected: commit succeeds. Leave unrelated `README.md` changes unstaged.

---

### Task 3: Latest Collector Baseline Tail Scan, Resume, Retry, And Corruption

**Files:**
- Create: `books_of_time/collectors/latest_comments.py`
- Create: `tests/test_latest_comments_worker.py`

**Interfaces:**
- Consumes:
  - `LatestCommentsClient.get_video_stats(bvid: str) -> FetchResult`
  - `LatestCommentsClient.get_latest_comments(aid: int, offset: str = "") -> FetchResult`
  - `RawPayloadRepository.insert_from_fetch_result`
  - `RawPageObservationRepository.insert_from_parsed_page`
  - `CommentRepository.upsert_page`
  - `FrontierStateRepository.get_or_create`
  - `parse_latest_comment_page`
- Produces:
  - `LatestCommentCollector.collect(task: CollectionTask, session: AsyncSession) -> None`
  - baseline status values: `baseline_paused`, `baseline_tail_complete`, `baseline_corrupted`
  - persisted retry fields: `failed_cursor`, `failed_reason`, `failed_attempts`
  - follow-up task enqueue for paused baseline scans

- [ ] **Step 1: Add fake client and baseline pause/resume tests**

Create `tests/test_latest_comments_worker.py` with this shared fixture code:

```python
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
    def __init__(self, pages: dict[str, bytes], failures: dict[str, list[Exception]] | None = None) -> None:
        self.pages = pages
        self.failures = failures or {}
        self.latest_offsets: list[str] = []

    async def get_video_stats(self, bvid: str) -> FetchResult:
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


async def build_worker_with_task(tmp_path, client, *, max_scan_seconds: float = 55, page_retry_attempts: int = 3, clock=None):
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
        page_retry_backoff_seconds=[0, 0, 0],
        monotonic=clock.monotonic if clock else None,
        sleep=lambda seconds: None,
    )
    worker = Worker(
        session_factory=session_factory,
        collectors={TaskKind.FETCH_LATEST_COMMENTS: collector},
        lease_owner="worker-test",
    )
    return engine, session_factory, worker, now
```

Append these tests:

```python
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
            await session.scalars(select(CollectionTask).order_by(CollectionTask.id.asc()))
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
async def test_baseline_resumes_from_saved_cursor_and_marks_tail_complete(tmp_path) -> None:
    client = FakeLatestClient(
        {
            "": latest_body(rpid=3003, next_offset="offset-2"),
            "offset-2": latest_body(rpid=3002, next_offset="", is_end=True),
        }
    )
    engine, session_factory, worker, now = await build_worker_with_task(tmp_path, client)

    await worker.run_once(now=now)

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))
        raw_pages = (
            await session.scalars(select(RawPageObservation).order_by(RawPageObservation.id.asc()))
        ).all()
        entity_count = await session.scalar(select(func.count(CommentEntity.rpid)))
        observation_count = await session.scalar(select(func.count(CommentObservation.id)))

        assert state is not None
        assert state.last_scan_status == "baseline_tail_complete"
        assert state.last_scan_truncated is False
        assert state.cursor == ""
        assert state.extra["baseline_status"] == "tail_complete"
        assert state.extra["baseline_start_frontier_rpid"] == 3003
        assert [page.cursor for page in raw_pages] == ["", "offset-2"]
        assert entity_count == 2
        assert observation_count == 2

    await engine.dispose()
```

- [ ] **Step 2: Add retry/corruption tests**

Append:

```python
@pytest.mark.asyncio
async def test_baseline_corrupted_when_same_cursor_fails_after_attempts(tmp_path) -> None:
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
async def test_failed_cursor_pauses_when_time_slice_expires_before_attempts_exhausted(tmp_path) -> None:
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
```

- [ ] **Step 3: Run new tests and verify failure**

Run:

```bash
uv run pytest tests/test_latest_comments_worker.py::test_baseline_pauses_at_time_budget_and_enqueues_followup tests/test_latest_comments_worker.py::test_baseline_resumes_from_saved_cursor_and_marks_tail_complete tests/test_latest_comments_worker.py::test_baseline_corrupted_when_same_cursor_fails_after_attempts tests/test_latest_comments_worker.py::test_failed_cursor_pauses_when_time_slice_expires_before_attempts_exhausted -v
```

Expected: fails because `books_of_time.collectors.latest_comments` does not exist.

- [ ] **Step 4: Implement latest collector baseline state machine**

Create `books_of_time/collectors/latest_comments.py` with these public interfaces and helpers:

```python
from __future__ import annotations

import inspect
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import CollectionTask, FrontierState, RawPayload
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    CommentRepository,
    FrontierStateRepository,
    RawPageObservationRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind
from books_of_time.http.client import FetchResult
from books_of_time.parsers.comments import (
    COMMENT_PARSER_VERSION,
    ParsedCommentPage,
    parse_latest_comment_page,
)
from books_of_time.storage.filesystem import RawPayloadFileStore

SleepFunc = Callable[[float], None | Awaitable[None]]
MonotonicFunc = Callable[[], float]


class LatestCommentsClient(Protocol):
    async def get_video_stats(self, bvid: str) -> FetchResult: ...

    async def get_latest_comments(self, *, aid: int, offset: str = "") -> FetchResult: ...


class LatestCommentCollector:
    def __init__(
        self,
        *,
        client: LatestCommentsClient,
        raw_store: RawPayloadFileStore,
        run_id: str,
        max_scan_seconds: float = 55,
        page_retry_attempts: int = 3,
        page_retry_backoff_seconds: list[float] | None = None,
        monotonic: MonotonicFunc | None = None,
        sleep: SleepFunc | None = None,
    ) -> None:
        self.client = client
        self.raw_store = raw_store
        self.run_id = run_id
        self.max_scan_seconds = max_scan_seconds
        self.page_retry_attempts = page_retry_attempts
        self.page_retry_backoff_seconds = page_retry_backoff_seconds or [1, 3, 5]
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep

    async def collect(self, task: CollectionTask, session: AsyncSession) -> None:
        bvid = str(task.payload.get("bvid") or task.target_id)
        aid = await self._resolve_aid(task, session, bvid)
        now = datetime.now(UTC)
        state = await FrontierStateRepository(session).get_or_create(
            target_type="video",
            target_id=bvid,
            frontier_type="latest_comments",
            now=now,
        )
        if state.extra.get("baseline_status") == "baseline_complete":
            await self._run_incremental(task, session, state, bvid=bvid, aid=aid)
            return
        if state.extra.get("baseline_status") == "tail_complete":
            await self._run_head_sweep(task, session, state, bvid=bvid, aid=aid)
            return
        await self._run_baseline_tail(task, session, state, bvid=bvid, aid=aid)
```

In the same file, implement these exact helper responsibilities:

```python
    async def _resolve_aid(
        self,
        task: CollectionTask,
        session: AsyncSession,
        bvid: str,
    ) -> int:
        aid = task.payload.get("aid")
        if aid is not None:
            return int(aid)
        video_result = await self.client.get_video_stats(bvid)
        video_raw = await self._archive_raw(video_result, session, parser_version=None)
        video_payload = json.loads(video_result.body)
        data = video_payload.get("data") or {}
        resolved = data.get("aid")
        if resolved is None:
            raise ValueError("Video info payload does not contain data.aid")
        task.payload = {
            **task.payload,
            "aid": int(resolved),
            "video_raw_payload_id": video_raw.id,
        }
        return int(resolved)

    async def _archive_raw(
        self,
        result: FetchResult,
        session: AsyncSession,
        *,
        parser_version: str | None,
    ) -> RawPayload:
        stored = self.raw_store.save(
            body=result.body,
            captured_at=result.captured_at,
            run_id=self.run_id,
            suffix=".json",
        )
        return await RawPayloadRepository(session).insert_from_fetch_result(
            result=result,
            stored=stored,
            parser_version=parser_version,
        )

    def _time_expired(self, started_at: float) -> bool:
        return self.monotonic() - started_at >= self.max_scan_seconds

    async def _sleep_for_attempt(self, attempt_index: int) -> None:
        seconds = self.page_retry_backoff_seconds[
            min(attempt_index, len(self.page_retry_backoff_seconds) - 1)
        ]
        result = self.sleep(seconds)
        if inspect.isawaitable(result):
            await result
```

Implement `_fetch_page_with_retry` so it:

```python
    async def _fetch_page_with_retry(
        self,
        *,
        state: FrontierState,
        aid: int,
        offset: str,
        started_at: float,
        baseline: bool,
    ) -> FetchResult | None:
        attempts = int(state.extra.get("failed_attempts") or 0)
        while attempts < self.page_retry_attempts:
            if self._time_expired(started_at):
                self._mark_paused_after_failed_attempts(
                    state,
                    cursor=offset,
                    reason=str(state.extra.get("failed_reason") or ""),
                    attempts=attempts,
                    baseline=baseline,
                )
                return None
            try:
                result = await self.client.get_latest_comments(aid=aid, offset=offset)
                state.extra.pop("failed_cursor", None)
                state.extra.pop("failed_reason", None)
                state.extra.pop("failed_attempts", None)
                return result
            except Exception as exc:
                attempts += 1
                state.extra["failed_cursor"] = offset
                state.extra["failed_reason"] = str(exc)
                state.extra["failed_attempts"] = attempts
                if attempts >= self.page_retry_attempts:
                    self._mark_corrupted(state, baseline=baseline)
                    return None
                if self._time_expired(started_at):
                    self._mark_paused_after_failed_attempts(
                        state,
                        cursor=offset,
                        reason=str(exc),
                        attempts=attempts,
                        baseline=baseline,
                    )
                    return None
                await self._sleep_for_attempt(attempts - 1)
        self._mark_corrupted(state, baseline=baseline)
        return None
```

Implement `_run_baseline_tail` with this behavior:

```python
    async def _run_baseline_tail(
        self,
        task: CollectionTask,
        session: AsyncSession,
        state: FrontierState,
        *,
        bvid: str,
        aid: int,
    ) -> None:
        started_at = self.monotonic()
        extra = dict(state.extra or {})
        extra.setdefault("baseline_started_at", datetime.now(UTC).isoformat())
        extra.setdefault("baseline_status", "baseline_paused")
        seen_cursors = list(extra.get("seen_cursors") or [])
        offset = str(state.cursor or extra.get("failed_cursor") or "")
        page_number = int(state.last_scan_pages or 0) + 1
        pages_this_run = 0
        state.extra = extra

        while True:
            if self._time_expired(started_at):
                self._mark_paused(state, cursor=offset, baseline=True)
                await self._enqueue_followup(session, task)
                return
            if offset in seen_cursors:
                state.extra["failed_cursor"] = offset
                state.extra["failed_reason"] = "cursor repeated"
                self._mark_corrupted(state, baseline=True)
                return
            seen_cursors.append(offset)
            state.extra["seen_cursors"] = seen_cursors

            result = await self._fetch_page_with_retry(
                state=state,
                aid=aid,
                offset=offset,
                started_at=started_at,
                baseline=True,
            )
            if result is None:
                if state.last_scan_status == "baseline_paused":
                    await self._enqueue_followup(session, task)
                return

            parsed = await self._persist_page(
                session,
                result,
                bvid=bvid,
                aid=aid,
                page_number=page_number,
                request_offset=offset,
            )
            pages_this_run += 1
            state.last_scan_pages = int(state.last_scan_pages or 0) + 1
            if state.extra.get("baseline_start_frontier_rpid") is None and parsed.comments:
                state.extra["baseline_start_frontier_rpid"] = parsed.comments[0].rpid
                state.extra["baseline_start_frontier_time"] = result.captured_at.isoformat()

            next_offset = str(parsed.extra["next_offset"])
            if parsed.extra["is_end"]:
                state.cursor = ""
                state.last_scan_at = result.captured_at
                state.last_scan_status = "baseline_tail_complete"
                state.last_scan_truncated = False
                state.extra["baseline_status"] = "tail_complete"
                state.extra["tail_completed_at"] = result.captured_at.isoformat()
                return

            offset = next_offset
            state.cursor = offset
            page_number += 1
```

Implement shared persistence and status helpers:

```python
    async def _persist_page(
        self,
        session: AsyncSession,
        result: FetchResult,
        *,
        bvid: str,
        aid: int,
        page_number: int,
        request_offset: str,
    ) -> ParsedCommentPage:
        raw = await self._archive_raw(
            result,
            session,
            parser_version=COMMENT_PARSER_VERSION,
        )
        parsed = parse_latest_comment_page(
            json.loads(result.body),
            bvid=bvid,
            oid=aid,
            captured_at=result.captured_at,
            raw_payload_id=raw.id,
            page_number=page_number,
            request_offset=request_offset,
        )
        raw_page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_LATEST,
        )
        await CommentRepository(session).upsert_page(
            parsed,
            raw_page_observation_id=raw_page.id,
        )
        return parsed

    async def _enqueue_followup(
        self,
        session: AsyncSession,
        task: CollectionTask,
    ) -> None:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type=task.target_type,
            target_id=task.target_id,
            priority=task.priority,
            payload={**task.payload, "mode": "latest"},
            not_before=datetime.now(UTC),
            budget_cost=task.budget_cost,
            max_retries=task.max_retries,
        )

    def _mark_paused(self, state: FrontierState, *, cursor: str, baseline: bool) -> None:
        state.cursor = cursor
        state.last_scan_at = datetime.now(UTC)
        state.last_scan_status = "baseline_paused" if baseline else "paused"
        state.last_scan_truncated = True
        if baseline:
            state.extra["baseline_status"] = "baseline_paused"

    def _mark_paused_after_failed_attempts(
        self,
        state: FrontierState,
        *,
        cursor: str,
        reason: str,
        attempts: int,
        baseline: bool,
    ) -> None:
        state.extra["failed_cursor"] = cursor
        state.extra["failed_reason"] = reason
        state.extra["failed_attempts"] = attempts
        self._mark_paused(state, cursor=cursor, baseline=baseline)

    def _mark_corrupted(self, state: FrontierState, *, baseline: bool) -> None:
        state.last_scan_at = datetime.now(UTC)
        state.last_scan_status = "baseline_corrupted" if baseline else "corrupted"
        state.last_scan_truncated = True
        if baseline:
            state.extra["baseline_status"] = "baseline_corrupted"
```

For this task, implement `_run_head_sweep` and `_run_incremental` as explicit no-op methods that raise only when called by an impossible state:

```python
    async def _run_head_sweep(self, task, session, state, *, bvid: str, aid: int) -> None:
        raise RuntimeError("unexpected latest comment head sweep state before tail completion")

    async def _run_incremental(self, task, session, state, *, bvid: str, aid: int) -> None:
        raise RuntimeError("unexpected latest comment incremental state before baseline completion")
```

- [ ] **Step 5: Run task verification**

Run:

```bash
uv run pytest tests/test_latest_comments_worker.py::test_baseline_pauses_at_time_budget_and_enqueues_followup tests/test_latest_comments_worker.py::test_baseline_resumes_from_saved_cursor_and_marks_tail_complete tests/test_latest_comments_worker.py::test_baseline_corrupted_when_same_cursor_fails_after_attempts tests/test_latest_comments_worker.py::test_failed_cursor_pauses_when_time_slice_expires_before_attempts_exhausted -v
uv run ruff check books_of_time/collectors/latest_comments.py tests/test_latest_comments_worker.py
```

Expected: selected tests pass and Ruff reports no issues.

- [ ] **Step 6: Commit task**

Run:

```bash
git add books_of_time/collectors/latest_comments.py tests/test_latest_comments_worker.py
git commit -m "feat: collect latest comment baseline tail"
```

Expected: commit succeeds. Leave unrelated `README.md` changes unstaged.

---

### Task 4: Head Sweep, Incremental Frontier, Frontier Missing, And Cursor Loop Detection

**Files:**
- Modify: `books_of_time/collectors/latest_comments.py`
- Modify: `tests/test_latest_comments_worker.py`

**Interfaces:**
- Consumes from Task 3:
  - `LatestCommentCollector._persist_page`
  - `FrontierState.extra["baseline_start_frontier_rpid"]`
  - `FrontierState.frontier_rpid`
- Produces:
  - baseline complete status `baseline_complete`
  - incremental complete status `incremental_complete`
  - missing old frontier status `frontier_missing`
  - cursor loop status `corrupted`
  - official `FrontierState.frontier_rpid` set to newest head-sweep or incremental comment

- [ ] **Step 1: Add head sweep and incremental tests**

Append to `tests/test_latest_comments_worker.py`:

```python
@pytest.mark.asyncio
async def test_head_sweep_completes_baseline_and_sets_frontier(tmp_path) -> None:
    client = FakeLatestClient(
        {
            "": latest_body(rpid=4001, next_offset="offset-2"),
            "offset-2": latest_body(rpid=3003, next_offset="offset-3"),
            "offset-3": latest_body(rpid=3002, next_offset="", is_end=True),
        }
    )
    engine, session_factory, worker, now = await build_worker_with_task(tmp_path, client)

    await worker.run_once(now=now)
    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type="video",
            target_id="BV1abc",
            priority=70,
            payload={"bvid": "BV1abc", "mode": "latest", "aid": 777},
            not_before=now - timedelta(seconds=1),
        )
        await session.commit()

    client.pages = {
        "": latest_body(rpid=4001, next_offset="offset-head-2"),
        "offset-head-2": latest_body(rpid=3003, next_offset="offset-head-3"),
    }
    await worker.run_once(now=now)

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))

        assert state is not None
        assert state.last_scan_status == "baseline_complete"
        assert state.last_scan_truncated is False
        assert state.frontier_rpid == 4001
        assert state.extra["baseline_status"] == "baseline_complete"
        assert "baseline_completed_at" in state.extra

    await engine.dispose()
```

Append incremental tests:

```python
@pytest.mark.asyncio
async def test_incremental_stops_at_old_frontier_and_updates_new_frontier(tmp_path) -> None:
    client = FakeLatestClient(
        {
            "": latest_body(rpid=5001, next_offset="offset-2"),
            "offset-2": latest_body(rpid=4001, next_offset="offset-3"),
        }
    )
    engine, session_factory, worker, now = await build_worker_with_task(tmp_path, client)
    async with session_factory() as session:
        state = FrontierState(
            target_type="video",
            target_id="BV1abc",
            frontier_type="latest_comments",
            frontier_rpid=4001,
            frontier_time=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            cursor=None,
            last_scan_at=None,
            last_scan_status="baseline_complete",
            last_scan_pages=0,
            last_scan_truncated=False,
            extra={"baseline_status": "baseline_complete"},
            created_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            updated_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        )
        session.add(state)
        await session.commit()

    await worker.run_once(now=now)

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))

        assert state is not None
        assert state.last_scan_status == "incremental_complete"
        assert state.last_scan_truncated is False
        assert state.frontier_rpid == 5001
        assert client.latest_offsets == ["" , "offset-2"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_incremental_frontier_missing_when_service_end_reached(tmp_path) -> None:
    client = FakeLatestClient(
        {
            "": latest_body(rpid=5001, next_offset="offset-2"),
            "offset-2": latest_body(rpid=5000, next_offset="", is_end=True),
        }
    )
    engine, session_factory, worker, now = await build_worker_with_task(tmp_path, client)
    async with session_factory() as session:
        state = FrontierState(
            target_type="video",
            target_id="BV1abc",
            frontier_type="latest_comments",
            frontier_rpid=4001,
            frontier_time=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            cursor=None,
            last_scan_at=None,
            last_scan_status="baseline_complete",
            last_scan_pages=0,
            last_scan_truncated=False,
            extra={"baseline_status": "baseline_complete"},
            created_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
            updated_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        )
        session.add(state)
        await session.commit()

    await worker.run_once(now=now)

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))

        assert state is not None
        assert state.last_scan_status == "frontier_missing"
        assert state.last_scan_truncated is False
        assert state.frontier_rpid == 5001
        assert state.extra["missing_frontier_rpid"] == 4001

    await engine.dispose()


@pytest.mark.asyncio
async def test_repeated_next_offset_marks_scan_corrupted(tmp_path) -> None:
    client = FakeLatestClient(
        {
            "": latest_body(rpid=5001, next_offset="offset-loop"),
            "offset-loop": latest_body(rpid=5000, next_offset="offset-loop"),
        }
    )
    engine, session_factory, worker, now = await build_worker_with_task(tmp_path, client)

    await worker.run_once(now=now)

    async with session_factory() as session:
        state = await session.scalar(select(FrontierState))

        assert state is not None
        assert state.last_scan_status == "baseline_corrupted"
        assert state.last_scan_truncated is True
        assert state.extra["failed_cursor"] == "offset-loop"
        assert state.extra["failed_reason"] == "cursor repeated"

    await engine.dispose()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_latest_comments_worker.py::test_head_sweep_completes_baseline_and_sets_frontier tests/test_latest_comments_worker.py::test_incremental_stops_at_old_frontier_and_updates_new_frontier tests/test_latest_comments_worker.py::test_incremental_frontier_missing_when_service_end_reached tests/test_latest_comments_worker.py::test_repeated_next_offset_marks_scan_corrupted -v
```

Expected: head sweep and incremental tests fail because `_run_head_sweep` and `_run_incremental` still raise.

- [ ] **Step 3: Implement head sweep**

Replace `_run_head_sweep` in `books_of_time/collectors/latest_comments.py`:

```python
    async def _run_head_sweep(
        self,
        task: CollectionTask,
        session: AsyncSession,
        state: FrontierState,
        *,
        bvid: str,
        aid: int,
    ) -> None:
        started_at = self.monotonic()
        offset = ""
        page_number = 1
        seen_cursors: list[str] = []
        baseline_start_rpid = int(state.extra["baseline_start_frontier_rpid"])
        newest_rpid: int | None = None

        while True:
            if self._time_expired(started_at):
                self._mark_paused(state, cursor=offset, baseline=True)
                await self._enqueue_followup(session, task)
                return
            if offset in seen_cursors:
                state.extra["failed_cursor"] = offset
                state.extra["failed_reason"] = "cursor repeated"
                self._mark_corrupted(state, baseline=True)
                return
            seen_cursors.append(offset)

            result = await self._fetch_page_with_retry(
                state=state,
                aid=aid,
                offset=offset,
                started_at=started_at,
                baseline=True,
            )
            if result is None:
                if state.last_scan_status == "baseline_paused":
                    await self._enqueue_followup(session, task)
                return

            parsed = await self._persist_page(
                session,
                result,
                bvid=bvid,
                aid=aid,
                page_number=page_number,
                request_offset=offset,
            )
            if newest_rpid is None and parsed.comments:
                newest_rpid = parsed.comments[0].rpid
            if any(comment.rpid == baseline_start_rpid for comment in parsed.comments):
                state.frontier_rpid = newest_rpid or baseline_start_rpid
                state.frontier_time = result.captured_at
                state.cursor = None
                state.last_scan_at = result.captured_at
                state.last_scan_status = "baseline_complete"
                state.last_scan_truncated = False
                state.extra["baseline_status"] = "baseline_complete"
                state.extra["baseline_completed_at"] = result.captured_at.isoformat()
                return
            if parsed.extra["is_end"]:
                state.extra["failed_reason"] = "baseline start frontier not reached during head sweep"
                self._mark_corrupted(state, baseline=True)
                return
            offset = str(parsed.extra["next_offset"])
            page_number += 1
```

- [ ] **Step 4: Implement incremental scan**

Replace `_run_incremental`:

```python
    async def _run_incremental(
        self,
        task: CollectionTask,
        session: AsyncSession,
        state: FrontierState,
        *,
        bvid: str,
        aid: int,
    ) -> None:
        started_at = self.monotonic()
        offset = str(state.cursor or "")
        page_number = 1
        seen_cursors: list[str] = []
        old_frontier = state.frontier_rpid
        newest_rpid: int | None = None

        while True:
            if self._time_expired(started_at):
                self._mark_paused(state, cursor=offset, baseline=False)
                await self._enqueue_followup(session, task)
                return
            if offset in seen_cursors:
                state.extra["failed_cursor"] = offset
                state.extra["failed_reason"] = "cursor repeated"
                self._mark_corrupted(state, baseline=False)
                return
            seen_cursors.append(offset)

            result = await self._fetch_page_with_retry(
                state=state,
                aid=aid,
                offset=offset,
                started_at=started_at,
                baseline=False,
            )
            if result is None:
                if state.last_scan_status == "paused":
                    await self._enqueue_followup(session, task)
                return

            parsed = await self._persist_page(
                session,
                result,
                bvid=bvid,
                aid=aid,
                page_number=page_number,
                request_offset=offset,
            )
            if newest_rpid is None and parsed.comments:
                newest_rpid = parsed.comments[0].rpid
            if old_frontier is not None and any(
                comment.rpid == old_frontier for comment in parsed.comments
            ):
                state.frontier_rpid = newest_rpid or old_frontier
                state.frontier_time = result.captured_at
                state.cursor = None
                state.last_scan_at = result.captured_at
                state.last_scan_status = "incremental_complete"
                state.last_scan_truncated = False
                return
            if parsed.extra["is_end"]:
                if newest_rpid is not None:
                    state.frontier_rpid = newest_rpid
                    state.frontier_time = result.captured_at
                state.cursor = None
                state.last_scan_at = result.captured_at
                state.last_scan_status = "frontier_missing"
                state.last_scan_truncated = False
                state.extra["missing_frontier_rpid"] = old_frontier
                return
            offset = str(parsed.extra["next_offset"])
            state.cursor = offset
            page_number += 1
```

- [ ] **Step 5: Run task verification**

Run:

```bash
uv run pytest tests/test_latest_comments_worker.py -v
uv run ruff check books_of_time/collectors/latest_comments.py tests/test_latest_comments_worker.py
```

Expected: all latest collector tests pass and Ruff reports no issues.

- [ ] **Step 6: Commit task**

Run:

```bash
git add books_of_time/collectors/latest_comments.py tests/test_latest_comments_worker.py
git commit -m "feat: advance latest comment frontier"
```

Expected: commit succeeds. Leave unrelated `README.md` changes unstaged.

---

### Task 5: Worker Registration, CLI Command, Tracking, And Full Verification

**Files:**
- Modify: `books_of_time/app.py`
- Modify: `books_of_time/cli.py`
- Create: `tests/test_cli.py`
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes:
  - `LatestCommentCollector`
  - `TaskKind.FETCH_LATEST_COMMENTS`
  - config key `latest_comments`
- Produces:
  - worker registration for latest comments
  - CLI command `bot collect-latest-comments BVxxxx`
  - CLI flags `--priority 70` and `--max-scan-seconds 55`
  - task payload `{"bvid": bvid, "mode": "latest"}` plus explicit `max_scan_seconds` when supplied by CLI
  - per-task `max_scan_seconds` override that applies only to that collector invocation and does not mutate the collector default for later tasks

- [ ] **Step 1: Add CLI parser test**

Create `tests/test_cli.py`:

```python
from books_of_time.cli import build_parser


def test_collect_latest_comments_parser_defaults() -> None:
    args = build_parser().parse_args(["collect-latest-comments", "BV1abc"])

    assert args.command == "collect-latest-comments"
    assert args.bvid == "BV1abc"
    assert args.priority == 70
    assert args.max_scan_seconds == 55


def test_collect_latest_comments_parser_accepts_overrides() -> None:
    args = build_parser().parse_args(
        [
            "collect-latest-comments",
            "BV1abc",
            "--priority",
            "90",
            "--max-scan-seconds",
            "12",
        ]
    )

    assert args.priority == 90
    assert args.max_scan_seconds == 12
```

- [ ] **Step 2: Run CLI parser tests and verify failure**

Run:

```bash
uv run pytest tests/test_cli.py -v
```

Expected: fails because the `collect-latest-comments` command does not exist.

- [ ] **Step 3: Register latest collector in app**

Modify imports in `books_of_time/app.py`:

```python
from books_of_time.collectors.latest_comments import LatestCommentCollector
```

Inside `build_worker`, add:

```python
    latest_comments_cfg = cfg.get("latest_comments", {})
```

Add this collector entry:

```python
            TaskKind.FETCH_LATEST_COMMENTS: LatestCommentCollector(
                client=client,
                raw_store=RawPayloadFileStore(raw_dir),
                run_id=run_id,
                max_scan_seconds=float(latest_comments_cfg.get("max_scan_seconds", 55)),
                page_retry_attempts=int(
                    latest_comments_cfg.get("page_retry_attempts", 3)
                ),
                page_retry_backoff_seconds=[
                    float(value)
                    for value in latest_comments_cfg.get(
                        "page_retry_backoff_seconds",
                        [1, 3, 5],
                    )
                ],
            ),
```

- [ ] **Step 4: Add CLI command and enqueue helper**

Modify `build_parser()` in `books_of_time/cli.py`:

```python
    latest_comments = subparsers.add_parser("collect-latest-comments")
    latest_comments.add_argument("bvid")
    latest_comments.add_argument("--priority", type=int, default=70)
    latest_comments.add_argument("--max-scan-seconds", type=float, default=55)
```

Add branch in `_run()` after the `video comments` branch:

```python
    if args.command == "collect-latest-comments":
        await _enqueue_latest_comments(
            cfg,
            args.bvid,
            args.priority,
            args.max_scan_seconds,
        )
        return
```

Add helper:

```python
async def _enqueue_latest_comments(
    cfg: dict,
    bvid: str,
    priority: int,
    max_scan_seconds: float,
) -> None:
    session_factory = build_session_factory(cfg)
    payload = {"bvid": bvid, "mode": "latest"}
    if max_scan_seconds != 55:
        payload["max_scan_seconds"] = max_scan_seconds
    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type="video",
            target_id=bvid,
            priority=priority,
            payload=payload,
            not_before=datetime.now(UTC),
        )
        await session.commit()
    logger.info("Queued latest comments task for %s", bvid)
```

Update `LatestCommentCollector.collect()` to honor explicit CLI/test override without mutating `self.max_scan_seconds`:

```python
        configured_max_scan_seconds = task.payload.get("max_scan_seconds")
        max_scan_seconds = (
            float(configured_max_scan_seconds)
            if configured_max_scan_seconds is not None
            else self.max_scan_seconds
        )
```

Update the internal time-budget helpers in `LatestCommentCollector` so this local value is passed through one scan:

```python
    def _time_expired(self, started_at: float, *, max_scan_seconds: float) -> bool:
        return self.monotonic() - started_at >= max_scan_seconds
```

Then pass `max_scan_seconds=max_scan_seconds` from `collect()` into `_run_baseline_tail`, `_run_head_sweep`, and `_run_incremental`, and from those methods into `_fetch_page_with_retry` and `_time_expired`. Keep `self.max_scan_seconds` as the immutable collector default configured by `build_worker`.

- [ ] **Step 5: Run app and CLI tests**

Run:

```bash
uv run pytest tests/test_cli.py tests/test_latest_comments_worker.py tests/test_task_queue.py -v
uv run python -c "from books_of_time.app import build_worker; print(build_worker)"
```

Expected: selected tests pass and the Python smoke command prints the `build_worker` function object.

- [ ] **Step 6: Update tracking document**

Modify `docs/TODO.md` so Phase 1B latest-comments items that are implemented by this plan are checked. Keep out-of-scope Phase 1C items unchecked:

```markdown
- [x] 建立最新评论 parser。
- [x] 实现最新评论 cursor/frontier 状态。
- [x] 实现首次 baseline tail scan。
- [x] 实现 baseline head sweep。
- [x] 实现最新评论增量 frontier 扫描。
- [x] 实现 page-level retry/backoff。
- [x] 实现 paused/corrupted 状态落库。
- [x] CLI 支持 `bot collect-latest-comments BVxxxx`。
```

If the exact item wording in `docs/TODO.md` differs, preserve the document's local wording and only change the checkbox state for the same completed behavior.

- [ ] **Step 7: Run final verification**

Run:

```bash
uv run pytest
uv run ruff check .
```

Expected:

```text
pytest: all tests passed
ruff: All checks passed!
```

- [ ] **Step 8: Commit task**

Run:

```bash
git add books_of_time/app.py books_of_time/cli.py books_of_time/collectors/latest_comments.py tests/test_cli.py docs/TODO.md
git commit -m "feat: enqueue latest comment collection"
```

Expected: commit succeeds. Leave unrelated `README.md` changes unstaged unless the user explicitly asks to include it.

---

## Plan Self-Review

Spec coverage:

- Lazy latest-comments platform method using `get_comments_lazy`: Task 1.
- No `page_limit` or request total-count semantics: Global Constraints and Task 1.
- Parser extracts latest comments, `request_offset`, `next_offset`, and end state: Task 1.
- Valid empty replies treated as an end page: Task 1.
- Malformed cursor raises `CommentParseError`: Task 1.
- Public user fields and readable content remain stored: Task 1 parser plus existing `CommentRepository`.
- `frontier_states.extra` JSON field: Task 2.
- Cursor stored in raw page observations: Task 2.
- Baseline pause within 55-second default time slice: Task 3.
- Baseline resume from saved cursor: Task 3.
- Page retry attempts and backoff bounded by time slice: Task 3.
- Failed cursor state saved before pause: Task 3.
- Exhausted page retries mark baseline corrupted: Task 3.
- Repeated cursor marks scan corrupted: Task 4.
- Baseline tail complete before head sweep: Task 3.
- Head sweep completes baseline and sets official frontier: Task 4.
- Incremental scan stops at old frontier and updates to newest comment: Task 4.
- Missing old frontier at service end becomes `frontier_missing`: Task 4.
- CLI enqueues `fetch_latest_comments`: Task 5.
- Final verification with pytest and Ruff: Task 5.

Placeholder scan:

- No `TBD`, `implement later`, or vague "add appropriate error handling" instructions remain.
- The string `TODO` appears only in the repository filename `docs/TODO.md`.
- Task 4 head-sweep test is a single complete code block with no manual splice instruction.

Type consistency:

- `ParsedCommentPage.extra["request_offset"]` feeds `RawPageObservation.cursor`.
- `FrontierState.extra` uses `dict[str, Any]`, matching existing `json_dict_type`.
- `LatestCommentsClient.get_latest_comments` matches `BilibiliPlatformClient.get_latest_comments`.
- Collector status values match the spec: `baseline_paused`, `baseline_tail_complete`, `baseline_complete`, `baseline_corrupted`, `incremental_complete`, `frontier_missing`, `paused`, and `corrupted`.
- CLI, app registration, and tests all use `TaskKind.FETCH_LATEST_COMMENTS`.

Known implementation notes:

- Full `collection_runs` tables remain out of scope for Phase 1B.
- Reply collection remains out of scope for Phase 1B.
- Request-layer 403/429/captcha taxonomy remains out of scope for Phase 1B.
