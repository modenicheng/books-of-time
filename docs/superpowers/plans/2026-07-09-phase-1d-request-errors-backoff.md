# Phase 1D Request Errors And Backoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add stable request failure categories and persisted request backoff state so failed requests delay retryable tasks and appear clearly in coverage.

**Architecture:** Request-layer code raises typed `RequestFailure`/`ParseFailure` values with stable `RequestErrorKind` categories. The worker catches those typed failures, writes failed coverage using the stable category, upserts `request_backoff_states`, and delays retryable tasks to the computed `backoff_until`. Collectors wrap parser boundaries as `ParseFailure`.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, curl-cffi, bilibili-api-python adapter, pytest-asyncio, Ruff.

## Global Constraints

- Do not bypass platform risk controls.
- Do not retry captcha or 403 aggressively.
- Do not hide request failures as empty result sets.
- Keep raw successful-response archiving unchanged.
- If an HTTP response exists for a failed request, attach the `FetchResult` to the typed error so later slices can archive failed raw payloads.
- Use explicit string categories instead of parsing exception messages in the worker.
- Keep Phase 1D scoped to one worker task at a time. Global scheduler avoidance of backoff windows can be added after worker loop exists.
- Do not stage or modify unrelated pre-existing `books_of_time/http/client.py` or `books_of_time/http/rate_limiter.py` changes except the intended Phase 1D edits to `books_of_time/http/client.py`.
- When committing Task 4, stage only the Phase 1D hunks in `books_of_time/http/client.py`; leave pre-existing `HttpMethod` typing and rate-limiter comment changes unstaged if they are still dirty.
- Execute inline in this main session unless the user explicitly asks for subagents.

---

## File Structure

- Create `books_of_time/http/errors.py`: `RequestErrorKind`, `RequestFailure`, `ParseFailure`, and response classification helpers.
- Modify `books_of_time/http/client.py`: classify transport timeout and HTTP failure responses.
- Modify `books_of_time/platforms/bilibili/request_client.py`: append failed `FetchResult` to capture context before re-raising.
- Modify `books_of_time/db/models.py`: add `RequestBackoffState`.
- Modify `books_of_time/db/repositories.py`: add `RequestBackoffRepository`.
- Modify `books_of_time/db/__init__.py`: export `RequestBackoffState`.
- Modify `books_of_time/worker.py`: typed failure handling and backoff delay.
- Modify current collectors to wrap parser boundaries as `ParseFailure`.
- Modify `docs/TODO.md`: mark P0 request errors/backoff items completed.
- Create `tests/test_request_errors.py`.
- Create `tests/test_request_backoff.py`.
- Modify `tests/test_worker_coverage.py`.
- Modify `tests/test_bilibili_client.py`.
- Modify selected collector worker tests for parse errors.

---

### Task 1: Request Error Types And Classification

**Files:**
- Create: `books_of_time/http/errors.py`
- Test: `tests/test_request_errors.py`

**Interfaces:**
- Produces: `RequestErrorKind(StrEnum)`.
- Produces: `RequestFailure(Exception)`.
- Produces: `ParseFailure(RequestFailure)`.
- Produces: `classify_failed_fetch(result: FetchResult) -> RequestFailure | None`.
- Produces: `parse_retry_after(headers: dict[str, str] | None) -> int | None`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_request_errors.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult
from books_of_time.http.errors import (
    ParseFailure,
    RequestErrorKind,
    RequestFailure,
    classify_failed_fetch,
    parse_retry_after,
)


def _result(status_code: int, body: bytes = b"{}", headers=None) -> FetchResult:
    return FetchResult(
        request_type=BilibiliRequestType.COMMENT_HOT,
        method="GET",
        url="https://api.bilibili.com/x/v2/reply",
        params={},
        status_code=status_code,
        body=body,
        captured_at=datetime(2099, 1, 1, tzinfo=UTC),
        response_headers=headers or {},
    )


def test_classifies_http_failure_statuses() -> None:
    assert classify_failed_fetch(_result(403)).kind == RequestErrorKind.FORBIDDEN
    assert classify_failed_fetch(_result(429)).kind == RequestErrorKind.RATE_LIMITED
    assert classify_failed_fetch(_result(503)).kind == RequestErrorKind.SERVER_ERROR


def test_classifies_captcha_and_risk_control_markers() -> None:
    assert classify_failed_fetch(_result(412)).kind == RequestErrorKind.CAPTCHA
    assert (
        classify_failed_fetch(_result(200, "需要验证码".encode())).kind
        == RequestErrorKind.CAPTCHA
    )
    assert (
        classify_failed_fetch(_result(200, "触发风控".encode())).kind
        == RequestErrorKind.CAPTCHA
    )


def test_success_response_has_no_failure() -> None:
    assert classify_failed_fetch(_result(200, b'{"code":0}')) is None


def test_retry_after_parses_integer_seconds_only() -> None:
    assert parse_retry_after({"Retry-After": "60"}) == 60
    assert parse_retry_after({"retry-after": "90"}) == 90
    assert parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}) is None
    assert parse_retry_after(None) is None


def test_parse_failure_uses_parse_error_kind() -> None:
    failure = ParseFailure(
        request_type=BilibiliRequestType.VIDEO_STATS,
        message="missing data.stat",
        status_code=200,
        fetch_result=_result(200),
    )

    assert isinstance(failure, RequestFailure)
    assert failure.kind == RequestErrorKind.PARSE_ERROR
    assert failure.status_code == 200
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_request_errors.py -v
```

Expected: FAIL because `books_of_time.http.errors` does not exist.

- [ ] **Step 3: Implement error module**

Create `books_of_time/http/errors.py`:

```python
from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from books_of_time.domain.enums import BilibiliRequestType

if TYPE_CHECKING:
    from books_of_time.http.client import FetchResult


class RequestErrorKind(StrEnum):
    TIMEOUT = "timeout"
    FORBIDDEN = "403"
    RATE_LIMITED = "429"
    CAPTCHA = "captcha"
    SERVER_ERROR = "5xx"
    PARSE_ERROR = "parse_error"


class RequestFailure(Exception):
    def __init__(
        self,
        *,
        kind: RequestErrorKind,
        request_type: BilibiliRequestType,
        message: str,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        fetch_result: FetchResult | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.request_type = request_type
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.fetch_result = fetch_result


class ParseFailure(RequestFailure):
    def __init__(
        self,
        *,
        request_type: BilibiliRequestType,
        message: str,
        status_code: int | None = None,
        fetch_result: FetchResult | None = None,
    ) -> None:
        super().__init__(
            kind=RequestErrorKind.PARSE_ERROR,
            request_type=request_type,
            message=message,
            status_code=status_code,
            fetch_result=fetch_result,
        )


def parse_retry_after(headers: dict[str, str] | None) -> int | None:
    if not headers:
        return None
    value = None
    for key, candidate in headers.items():
        if key.lower() == "retry-after":
            value = candidate
            break
    if value is None or not value.isdigit():
        return None
    return int(value)


def classify_failed_fetch(result: FetchResult) -> RequestFailure | None:
    kind = _classify_kind(result.status_code, result.body)
    if kind is None:
        return None
    return RequestFailure(
        kind=kind,
        request_type=result.request_type,
        message=f"{result.request_type.value} failed with {kind.value}",
        status_code=result.status_code,
        retry_after_seconds=parse_retry_after(result.response_headers),
        fetch_result=result,
    )


def _classify_kind(status_code: int, body: bytes) -> RequestErrorKind | None:
    if status_code == 403:
        return RequestErrorKind.FORBIDDEN
    if status_code == 429:
        return RequestErrorKind.RATE_LIMITED
    if status_code == 412:
        return RequestErrorKind.CAPTCHA
    if 500 <= status_code <= 599:
        return RequestErrorKind.SERVER_ERROR

    text = body.decode("utf-8", errors="ignore").lower()
    if "captcha" in text or "验证码" in text or "风控" in text:
        return RequestErrorKind.CAPTCHA
    return None
```

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/test_request_errors.py -v
uv run ruff check books_of_time/http/errors.py tests/test_request_errors.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/http/errors.py tests/test_request_errors.py
git commit -m "feat: classify request failures"
```

---

### Task 2: Request Backoff State Repository

**Files:**
- Modify: `books_of_time/db/models.py`
- Modify: `books_of_time/db/repositories.py`
- Modify: `books_of_time/db/__init__.py`
- Test: `tests/test_request_backoff.py`

**Interfaces:**
- Consumes: `RequestFailure`, `RequestErrorKind`.
- Produces: `RequestBackoffRepository.record_failure(platform: str, scope: str, failure: RequestFailure, now: datetime, default_seconds: Mapping[str, int], max_seconds: int) -> RequestBackoffState`.

- [ ] **Step 1: Write failing repository test**

Create `tests/test_request_backoff.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import RequestBackoffState
from books_of_time.db.repositories import RequestBackoffRepository
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.errors import RequestErrorKind, RequestFailure


async def _create_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_request_backoff_records_and_updates_failure_state() -> None:
    engine, session_factory = await _create_session_factory()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    defaults = {"429": 10}
    try:
        async with session_factory() as session:
            repo = RequestBackoffRepository(session)
            failure = RequestFailure(
                kind=RequestErrorKind.RATE_LIMITED,
                request_type=BilibiliRequestType.COMMENT_HOT,
                message="rate limited",
                status_code=429,
            )
            first = await repo.record_failure(
                platform="bilibili",
                scope="global",
                failure=failure,
                now=now,
                default_seconds=defaults,
                max_seconds=1000,
            )
            second = await repo.record_failure(
                platform="bilibili",
                scope="global",
                failure=failure,
                now=now + timedelta(seconds=1),
                default_seconds=defaults,
                max_seconds=1000,
            )
            await session.commit()

        async with session_factory() as session:
            saved = await session.scalar(select(RequestBackoffState))
            assert first.id == second.id
            assert saved is not None
            assert saved.platform == "bilibili"
            assert saved.request_type == BilibiliRequestType.COMMENT_HOT
            assert saved.error_kind == "429"
            assert saved.status_code == 429
            assert saved.fail_count == 2
            assert saved.first_failed_at == now
            assert saved.last_failed_at == now + timedelta(seconds=1)
            assert saved.backoff_until == now + timedelta(seconds=21)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_request_backoff_uses_retry_after_before_default() -> None:
    engine, session_factory = await _create_session_factory()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            state = await RequestBackoffRepository(session).record_failure(
                platform="bilibili",
                scope="global",
                failure=RequestFailure(
                    kind=RequestErrorKind.RATE_LIMITED,
                    request_type=BilibiliRequestType.DEFAULT,
                    message="retry later",
                    status_code=429,
                    retry_after_seconds=45,
                ),
                now=now,
                default_seconds={"429": 10},
                max_seconds=1000,
            )

            assert state.retry_after_seconds == 45
            assert state.backoff_until == now + timedelta(seconds=45)
    finally:
        await engine.dispose()
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_request_backoff.py -v
```

Expected: FAIL because `RequestBackoffState` and repository do not exist.

- [ ] **Step 3: Add ORM model**

Add `RequestBackoffState` to `books_of_time/db/models.py` after `CollectionCoverageStat`:

```python
class RequestBackoffState(TimestampMixin, Base):
    __tablename__ = "request_backoff_states"
    __table_args__ = (
        UniqueConstraint("platform", "request_type", "scope"),
    )

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    request_type: Mapped[BilibiliRequestType] = mapped_column(
        Enum(BilibiliRequestType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    error_kind: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_failed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_failed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    backoff_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_message: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index(
    "idx_request_backoff_key",
    RequestBackoffState.platform,
    RequestBackoffState.request_type,
    RequestBackoffState.scope,
)
Index("idx_request_backoff_until", RequestBackoffState.backoff_until)
Index(
    "idx_request_backoff_error_time",
    RequestBackoffState.error_kind,
    RequestBackoffState.last_failed_at.desc(),
)
```

- [ ] **Step 4: Add repository**

Add imports to `books_of_time/db/repositories.py`:

```python
from collections.abc import Mapping
from books_of_time.http.errors import RequestFailure
```

Add `RequestBackoffState` to model imports.

Add class:

```python
class RequestBackoffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_failure(
        self,
        *,
        platform: str,
        scope: str,
        failure: RequestFailure,
        now: datetime,
        default_seconds: Mapping[str, int],
        max_seconds: int,
    ) -> RequestBackoffState:
        state = await self.session.scalar(
            select(RequestBackoffState).where(
                RequestBackoffState.platform == platform,
                RequestBackoffState.request_type == failure.request_type,
                RequestBackoffState.scope == scope,
            )
        )
        if state is None:
            state = RequestBackoffState(
                platform=platform,
                request_type=failure.request_type,
                scope=scope,
                error_kind=failure.kind.value,
                status_code=failure.status_code,
                retry_after_seconds=failure.retry_after_seconds,
                fail_count=0,
                first_failed_at=now,
                last_failed_at=now,
                backoff_until=now,
                last_message=str(failure),
                extra={},
                created_at=now,
                updated_at=now,
            )
            self.session.add(state)

        state.fail_count += 1
        state.error_kind = failure.kind.value
        state.status_code = failure.status_code
        state.retry_after_seconds = failure.retry_after_seconds
        state.last_failed_at = now
        state.last_message = str(failure)
        state.backoff_until = now + timedelta(
            seconds=_backoff_seconds(
                failure=failure,
                fail_count=state.fail_count,
                default_seconds=default_seconds,
                max_seconds=max_seconds,
            )
        )
        state.updated_at = now
        await self.session.flush()
        return state
```

Add helper:

```python
def _backoff_seconds(
    *,
    failure: RequestFailure,
    fail_count: int,
    default_seconds: Mapping[str, int],
    max_seconds: int,
) -> int:
    base = failure.retry_after_seconds
    if base is None:
        base = int(default_seconds.get(failure.kind.value, 300))
    multiplier = 2 ** min(max(fail_count - 1, 0), 5)
    return min(int(base) * multiplier, max_seconds)
```

- [ ] **Step 5: Export model**

Update `books_of_time/db/__init__.py` to export `RequestBackoffState`.

- [ ] **Step 6: Verify GREEN**

```bash
uv run pytest tests/test_request_backoff.py tests/test_coverage_repositories.py -v
uv run ruff check books_of_time/db/models.py books_of_time/db/repositories.py books_of_time/db/__init__.py tests/test_request_backoff.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add books_of_time/db/models.py books_of_time/db/repositories.py books_of_time/db/__init__.py tests/test_request_backoff.py
git commit -m "feat: persist request backoff states"
```

---

### Task 3: Worker Typed Failure Backoff

**Files:**
- Modify: `books_of_time/worker.py`
- Modify: `tests/test_worker_coverage.py`

**Interfaces:**
- Consumes: `RequestFailure`.
- Consumes: `RequestBackoffRepository.record_failure(...)`.
- Produces: `Worker(..., request_backoff_defaults: Mapping[str, int] | None = None, request_backoff_max_seconds: int = 21600)`.

- [ ] **Step 1: Write failing worker test**

Add to `tests/test_worker_coverage.py`:

```python
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.db.models import RequestBackoffState
from books_of_time.http.errors import RequestErrorKind, RequestFailure


class RateLimitedCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        raise RequestFailure(
            kind=RequestErrorKind.RATE_LIMITED,
            request_type=BilibiliRequestType.VIDEO_STATS,
            message="rate limited",
            status_code=429,
            retry_after_seconds=45,
        )
```

```python
@pytest.mark.asyncio
async def test_worker_uses_request_failure_backoff_for_retry(session_factory) -> None:
    ...
```

Assert:

```python
assert stat.status == "failed"
assert stat.reason == "429"
assert task.status == TaskStatus.PENDING
assert task.not_before == now + timedelta(seconds=45)
assert backoff.error_kind == "429"
assert backoff.backoff_until == now + timedelta(seconds=45)
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_worker_coverage.py -v
```

Expected: FAIL because worker handles typed request failures as generic collector exceptions.

- [ ] **Step 3: Implement worker typed branch**

Modify `books_of_time/worker.py` imports:

```python
from collections.abc import Mapping
from books_of_time.db.repositories import RequestBackoffRepository
from books_of_time.http.errors import RequestFailure
```

Add init args:

```python
request_backoff_defaults: Mapping[str, int] | None = None,
request_backoff_max_seconds: int = 21600,
```

Default:

```python
self.request_backoff_defaults = dict(
    request_backoff_defaults
    or {
        "timeout": 60,
        "403": 1800,
        "429": 900,
        "captcha": 3600,
        "5xx": 300,
        "parse_error": 300,
    }
)
self.request_backoff_max_seconds = request_backoff_max_seconds
```

In `except Exception as exc`, branch first:

```python
if isinstance(exc, RequestFailure):
    backoff = await RequestBackoffRepository(session).record_failure(
        platform="bilibili",
        scope="global",
        failure=exc,
        now=effective_now,
        default_seconds=self.request_backoff_defaults,
        max_seconds=self.request_backoff_max_seconds,
    )
    await coverage_repo.insert_failed(
        task=task,
        run_id=self.run_id,
        started_at=effective_now,
        finished_at=finished_at,
        reason=exc.kind.value,
        extra={
            "exception_type": type(exc).__name__,
            "request_type": exc.request_type.value,
            "status_code": exc.status_code,
        },
    )
    ...
    task.not_before = backoff.backoff_until
else:
    existing generic branch
```

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/test_worker_coverage.py tests/test_request_backoff.py -v
uv run ruff check books_of_time/worker.py tests/test_worker_coverage.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/worker.py tests/test_worker_coverage.py
git commit -m "feat: apply request backoff in worker"
```

---

### Task 4: Request Layer Integration

**Files:**
- Modify: `books_of_time/http/client.py`
- Modify: `books_of_time/platforms/bilibili/request_client.py`
- Modify: `tests/test_bilibili_client.py`
- Test: `tests/test_request_errors.py`

**Interfaces:**
- Consumes: `classify_failed_fetch`.
- Produces: `RawHttpClient.request(...)` raises `RequestFailure` for timeout and classified HTTP responses.
- Produces: Bilibili request capture context stores failed `FetchResult` when present.

- [ ] **Step 1: Add failing request-layer tests**

In `tests/test_request_errors.py`, add:

```python
import pytest

from books_of_time.http.errors import RequestFailure


class FakeTimeoutSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def request(self, *args, **kwargs):
        raise TimeoutError("timed out")


@pytest.mark.asyncio
async def test_raw_http_client_maps_timeout(monkeypatch) -> None:
    monkeypatch.setattr("books_of_time.http.client.AsyncSession", FakeTimeoutSession)
    client = RawHttpClient(timeout_seconds=1)

    with pytest.raises(RequestFailure) as exc_info:
        await client.request(
            method="GET",
            url="https://api.bilibili.com/x/test",
            request_type=BilibiliRequestType.DEFAULT,
        )

    assert exc_info.value.kind == RequestErrorKind.TIMEOUT
```

Add a fake 429 response test with `AsyncSession` returning status `429` and
`Retry-After: 45`, then assert `RequestFailure.kind == RATE_LIMITED` and
`fetch_result is not None`.

In `tests/test_bilibili_client.py`, add a fake raw client that raises
`RequestFailure` with attached `FetchResult`, then call through
`capture_bili_api_requests` and assert the context captured that failed result
before the failure propagated.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_request_errors.py tests/test_bilibili_client.py -v
```

Expected: FAIL because `RawHttpClient` and request adapter do not classify typed failures.

- [ ] **Step 3: Implement RawHttpClient classification**

Modify `books_of_time/http/client.py`:

```python
from books_of_time.http.errors import (
    RequestErrorKind,
    RequestFailure,
    classify_failed_fetch,
)
```

Wrap `session.request(...)`:

```python
try:
    response = await session.request(...)
except TimeoutError as exc:
    raise RequestFailure(
        kind=RequestErrorKind.TIMEOUT,
        request_type=request_type,
        message=str(exc),
    ) from exc
```

After building `FetchResult`:

```python
failure = classify_failed_fetch(result)
if failure is not None:
    raise failure
return result
```

Keep the existing `HttpMethod` annotation and comments in this file; they are
pre-existing working-tree changes that must be preserved.

- [ ] **Step 4: Capture failed FetchResult in adapter**

Modify `books_of_time/platforms/bilibili/request_client.py`:

```python
from books_of_time.http.errors import RequestFailure
```

Around `context.http_client.request(...)`:

```python
try:
    result = await context.http_client.request(...)
except RequestFailure as exc:
    if exc.fetch_result is not None:
        context.captured_results.append(exc.fetch_result)
    raise
```

- [ ] **Step 5: Verify GREEN**

```bash
uv run pytest tests/test_request_errors.py tests/test_bilibili_client.py -v
uv run ruff check books_of_time/http/client.py books_of_time/platforms/bilibili/request_client.py tests/test_request_errors.py tests/test_bilibili_client.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add books_of_time/http/client.py books_of_time/platforms/bilibili/request_client.py tests/test_request_errors.py tests/test_bilibili_client.py
git commit -m "feat: surface typed request failures"
```

---

### Task 5: Collector Parse Failures, Docs, And Full Verification

**Files:**
- Modify: `books_of_time/collectors/video_stats.py`
- Modify: `books_of_time/collectors/hot_comments.py`
- Modify: `books_of_time/collectors/latest_comments.py`
- Modify: selected collector tests
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: `ParseFailure`.
- Produces: parser boundary failures that worker records as `parse_error`.

- [ ] **Step 1: Add failing parse-error test**

Add one focused test to `tests/test_video_stats_worker.py` with malformed stats
body. Assert:

```python
with pytest.raises(ParseFailure):
    await worker.run_once(now=now)

coverage = await session.scalar(select(CollectionCoverageStat))
backoff = await session.scalar(select(RequestBackoffState))
task = await session.scalar(select(CollectionTask))
assert coverage.reason == "parse_error"
assert backoff.error_kind == "parse_error"
assert task.status == TaskStatus.PENDING
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_video_stats_worker.py -v
```

Expected: FAIL because parse errors are currently generic exceptions.

- [ ] **Step 3: Wrap parser boundaries**

In each collector, import:

```python
from books_of_time.http.errors import ParseFailure
```

Wrap parser calls:

```python
try:
    parsed = parse_video_stats(...)
except Exception as exc:
    raise ParseFailure(
        request_type=result.request_type,
        message=str(exc),
        status_code=result.status_code,
        fetch_result=result,
    ) from exc
```

Apply the same pattern to `_extract_aid`, `parse_hot_comment_page`, aid
extraction in latest comments, and `parse_latest_comment_page`.

- [ ] **Step 4: Update TODO**

In `docs/TODO.md`, mark:

```markdown
- [x] 为请求失败建立统一错误类型：timeout、403、429、captcha、5xx、parse_error。
- [x] 建立 `request_backoff_states` 表。
- [x] 将失败退避接入 worker 和 request layer。
```

- [ ] **Step 5: Full verification**

```bash
uv run pytest
uv run ruff check .
```

Expected:

```text
49+ passed
All checks passed!
```

- [ ] **Step 6: Commit**

```bash
git add books_of_time/collectors/video_stats.py books_of_time/collectors/hot_comments.py books_of_time/collectors/latest_comments.py tests/test_video_stats_worker.py docs/TODO.md
git commit -m "feat: record parse failures as request backoff"
```

---

## Self-Review

Spec coverage:

- Stable error categories: Task 1.
- HTTP timeout/response classification: Task 4.
- `request_backoff_states`: Task 2.
- Worker typed failure backoff: Task 3.
- Coverage reason categories: Task 3 and Task 5.
- Collector parse boundaries: Task 5.
- TODO and full verification: Task 5.

Placeholder scan:

- No `TBD`, no open-ended "handle appropriately", and no task without concrete verification commands.

Type consistency:

- `RequestErrorKind` values match TODO wording.
- `RequestBackoffRepository.record_failure(...)` returns `RequestBackoffState`, which the worker uses for task `not_before`.
- `ParseFailure` is a `RequestFailure`, so the worker typed branch handles it without a second path.

Execution choice:

Execute inline in the main session. Do not dispatch subagents unless the user explicitly asks for them again.
