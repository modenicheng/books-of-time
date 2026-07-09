# Phase 1C Coverage And Data Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable collection run and per-task coverage summaries for existing video stats, hot comments, and latest comments collection.

**Architecture:** Keep coverage persistence in the worker transaction. Collectors return a `CoverageDraft` with domain counters, and the worker writes `collection_coverage_stats`, updates `collection_runs`, and then commits task status. The CLI reads coverage rows only; it never triggers collection.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, pytest-asyncio, argparse CLI, Ruff.

## Global Constraints

- Keep coverage factual; do not infer deletion, hidden state, or platform truth beyond observed responses.
- One `collection_coverage_stats` row is written for each collection task execution.
- `collection_runs` is created lazily when a worker instance executes its first task.
- Worker-level collector exceptions produce failed coverage while preserving existing retry behavior.
- Do not implement event-level coverage aggregation in this slice.
- Do not implement request-layer error taxonomy or global request backoff state in this slice.
- Preserve existing raw payload and raw page observation flow.
- Do not stage or modify unrelated `books_of_time/http/client.py` or `books_of_time/http/rate_limiter.py` working-tree changes.

---

## File Structure

- Create `books_of_time/coverage.py`: `CoverageDraft` dataclass and helpers for failed coverage.
- Modify `books_of_time/db/models.py`: add `CollectionRun` and `CollectionCoverageStat` ORM models plus indexes.
- Modify `books_of_time/db/repositories.py`: add `CollectionRunRepository` and `CollectionCoverageRepository`.
- Modify `books_of_time/db/__init__.py`: export new ORM models.
- Modify `books_of_time/worker.py`: widen collector protocol, lazily create run row, persist coverage rows, update run counters.
- Modify `books_of_time/app.py`: pass `run_id` into `Worker`.
- Modify `books_of_time/collectors/video_stats.py`: return `CoverageDraft`.
- Modify `books_of_time/collectors/hot_comments.py`: return `CoverageDraft`.
- Modify `books_of_time/collectors/latest_comments.py`: return `CoverageDraft` for baseline, incremental, paused, frontier missing, and corrupted outcomes.
- Modify `books_of_time/cli.py`: add `bot coverage BVxxxx`.
- Modify `docs/TODO.md`: mark completed Phase 1C items after implementation.
- Create `tests/test_coverage_repositories.py`: model and repository coverage.
- Create `tests/test_worker_coverage.py`: worker success and exception coverage.
- Modify `tests/test_video_stats_worker.py`, `tests/test_hot_comments_worker.py`, `tests/test_latest_comments_worker.py`, and `tests/test_cli.py`.

---

### Task 1: Coverage Models And Repositories

**Files:**
- Create: `books_of_time/coverage.py`
- Modify: `books_of_time/db/models.py`
- Modify: `books_of_time/db/repositories.py`
- Modify: `books_of_time/db/__init__.py`
- Test: `tests/test_coverage_repositories.py`

**Interfaces:**
- Produces: `CoverageDraft` dataclass.
- Produces: `CollectionRunRepository.get_or_create_running(run_id: str, worker_id: str, now: datetime) -> CollectionRun`.
- Produces: `CollectionRunRepository.record_task_started(run: CollectionRun, now: datetime) -> None`.
- Produces: `CollectionRunRepository.record_task_succeeded(run: CollectionRun, now: datetime) -> None`.
- Produces: `CollectionRunRepository.record_task_failed(run: CollectionRun, now: datetime) -> None`.
- Produces: `CollectionCoverageRepository.insert_from_draft(task: CollectionTask, run_id: str, draft: CoverageDraft, started_at: datetime, finished_at: datetime) -> CollectionCoverageStat`.
- Produces: `CollectionCoverageRepository.insert_failed(task: CollectionTask, run_id: str, started_at: datetime, finished_at: datetime, reason: str, extra: dict[str, Any] | None = None) -> CollectionCoverageStat`.
- Produces: `CollectionCoverageRepository.list_for_target(target_type: str, target_id: str, limit: int = 20) -> list[CollectionCoverageStat]`.

- [ ] **Step 1: Write failing repository tests**

Create `tests/test_coverage_repositories.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.coverage import CoverageDraft
from books_of_time.db.base import Base
from books_of_time.db.models import CollectionCoverageStat, CollectionRun
from books_of_time.db.repositories import (
    CollectionCoverageRepository,
    CollectionRunRepository,
    CollectionTaskRepository,
)
from books_of_time.domain.enums import TaskKind


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collection_run_repository_creates_and_updates_counts(
    session_factory,
) -> None:
    now = datetime(2099, 1, 1, tzinfo=UTC)
    async with session_factory() as session:
        repo = CollectionRunRepository(session)

        run = await repo.get_or_create_running(
            run_id="run-1",
            worker_id="worker-1",
            now=now,
        )
        await repo.record_task_started(run, now=now + timedelta(seconds=1))
        await repo.record_task_succeeded(run, now=now + timedelta(seconds=2))
        await session.commit()

    async with session_factory() as session:
        saved = await session.scalar(select(CollectionRun))
        assert saved is not None
        assert saved.run_id == "run-1"
        assert saved.worker_id == "worker-1"
        assert saved.status == "succeeded"
        assert saved.tasks_started == 1
        assert saved.tasks_succeeded == 1
        assert saved.tasks_failed == 0
        assert saved.finished_at == now + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_collection_coverage_repository_inserts_and_lists_by_target(
    session_factory,
) -> None:
    now = datetime(2099, 1, 1, tzinfo=UTC)
    async with session_factory() as session:
        task = await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id="BV1xx",
            priority=80,
            payload={"bvid": "BV1xx"},
            not_before=now,
        )
        draft = CoverageDraft(
            task_kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id="BV1xx",
            pages_requested=1,
            pages_succeeded=1,
            items_observed=2,
            raw_payloads_saved=2,
            reason="complete",
        )
        await CollectionCoverageRepository(session).insert_from_draft(
            task=task,
            run_id="run-1",
            draft=draft,
            started_at=now,
            finished_at=now + timedelta(seconds=3),
        )
        await session.commit()

    async with session_factory() as session:
        rows = await CollectionCoverageRepository(session).list_for_target(
            target_type="video",
            target_id="BV1xx",
        )
        assert len(rows) == 1
        assert rows[0].status == "succeeded"
        assert rows[0].pages_requested == 1
        assert rows[0].pages_succeeded == 1
        assert rows[0].items_observed == 2
        assert rows[0].reason == "complete"
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run pytest tests/test_coverage_repositories.py -v
```

Expected: FAIL because `books_of_time.coverage`, `CollectionRun`, `CollectionCoverageStat`, and repositories do not exist.

- [ ] **Step 3: Add `CoverageDraft`**

Create `books_of_time/coverage.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from books_of_time.domain.enums import TaskKind


@dataclass(slots=True)
class CoverageDraft:
    task_kind: TaskKind
    target_type: str
    target_id: str
    pages_requested: int = 0
    pages_succeeded: int = 0
    items_observed: int = 0
    raw_payloads_saved: int = 0
    parse_errors: int = 0
    request_errors: int = 0
    frontier_reached: bool | None = None
    frontier_missing: bool | None = None
    truncated: bool = False
    corrupted: bool = False
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.corrupted:
            return "corrupted"
        if self.reason in {"time_budget", "frontier_missing"} or self.truncated:
            return "partial"
        return "succeeded"
```

- [ ] **Step 4: Add ORM models**

Modify `books_of_time/db/models.py` after `CollectionTask` indexes and before `KnownVideo`:

```python
class CollectionRun(TimestampMixin, Base):
    __tablename__ = "collection_runs"
    __table_args__ = (UniqueConstraint("run_id"),)

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    tasks_started: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index("idx_collection_runs_run_id", CollectionRun.run_id)
Index("idx_collection_runs_started_at", CollectionRun.started_at.desc())


class CollectionCoverageStat(TimestampMixin, Base):
    __tablename__ = "collection_coverage_stats"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    collection_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_kind: Mapped[TaskKind] = mapped_column(
        Enum(TaskKind, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    pages_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_payloads_saved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parse_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frontier_reached: Mapped[bool | None] = mapped_column(Boolean)
    frontier_missing: Mapped[bool | None] = mapped_column(Boolean)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    corrupted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index(
    "idx_collection_coverage_target_time",
    CollectionCoverageStat.target_type,
    CollectionCoverageStat.target_id,
    CollectionCoverageStat.finished_at.desc(),
)
Index(
    "idx_collection_coverage_task",
    CollectionCoverageStat.collection_task_id,
)
Index("idx_collection_coverage_run", CollectionCoverageStat.run_id)
```

- [ ] **Step 5: Add repositories**

Modify `books_of_time/db/repositories.py` imports:

```python
from books_of_time.coverage import CoverageDraft
```

Add `CollectionRun` and `CollectionCoverageStat` to the model import list.

Append these repository classes after `CollectionTaskRepository`:

```python
class CollectionRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_running(
        self,
        *,
        run_id: str,
        worker_id: str,
        now: datetime,
    ) -> CollectionRun:
        run = await self.session.scalar(
            select(CollectionRun).where(CollectionRun.run_id == run_id)
        )
        if run is not None:
            return run

        run = CollectionRun(
            run_id=run_id,
            worker_id=worker_id,
            started_at=now,
            finished_at=None,
            status="running",
            tasks_started=0,
            tasks_succeeded=0,
            tasks_failed=0,
            extra={},
            created_at=now,
            updated_at=now,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def record_task_started(self, run: CollectionRun, *, now: datetime) -> None:
        run.tasks_started += 1
        run.status = "running"
        run.finished_at = None
        run.updated_at = now
        await self.session.flush()

    async def record_task_succeeded(
        self,
        run: CollectionRun,
        *,
        now: datetime,
    ) -> None:
        run.tasks_succeeded += 1
        run.status = "succeeded"
        run.finished_at = now
        run.updated_at = now
        await self.session.flush()

    async def record_task_failed(self, run: CollectionRun, *, now: datetime) -> None:
        run.tasks_failed += 1
        run.status = "failed"
        run.finished_at = now
        run.updated_at = now
        await self.session.flush()


class CollectionCoverageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_from_draft(
        self,
        *,
        task: CollectionTask,
        run_id: str,
        draft: CoverageDraft,
        started_at: datetime,
        finished_at: datetime,
    ) -> CollectionCoverageStat:
        stat = CollectionCoverageStat(
            collection_task_id=task.id,
            run_id=run_id,
            task_kind=draft.task_kind,
            target_type=draft.target_type,
            target_id=draft.target_id,
            started_at=started_at,
            finished_at=finished_at,
            status=draft.status,
            pages_requested=draft.pages_requested,
            pages_succeeded=draft.pages_succeeded,
            items_observed=draft.items_observed,
            raw_payloads_saved=draft.raw_payloads_saved,
            parse_errors=draft.parse_errors,
            request_errors=draft.request_errors,
            frontier_reached=draft.frontier_reached,
            frontier_missing=draft.frontier_missing,
            truncated=draft.truncated,
            corrupted=draft.corrupted,
            reason=draft.reason,
            extra=draft.extra,
            created_at=finished_at,
            updated_at=finished_at,
        )
        self.session.add(stat)
        await self.session.flush()
        return stat

    async def insert_failed(
        self,
        *,
        task: CollectionTask,
        run_id: str,
        started_at: datetime,
        finished_at: datetime,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> CollectionCoverageStat:
        draft = CoverageDraft(
            task_kind=task.kind,
            target_type=task.target_type,
            target_id=task.target_id,
            request_errors=1,
            corrupted=False,
            reason=reason,
            extra=extra or {},
        )
        stat = await self.insert_from_draft(
            task=task,
            run_id=run_id,
            draft=draft,
            started_at=started_at,
            finished_at=finished_at,
        )
        stat.status = "failed"
        await self.session.flush()
        return stat

    async def list_for_target(
        self,
        *,
        target_type: str,
        target_id: str,
        limit: int = 20,
    ) -> list[CollectionCoverageStat]:
        rows = await self.session.scalars(
            select(CollectionCoverageStat)
            .where(
                CollectionCoverageStat.target_type == target_type,
                CollectionCoverageStat.target_id == target_id,
            )
            .order_by(CollectionCoverageStat.finished_at.desc())
            .limit(limit)
        )
        return list(rows)
```

- [ ] **Step 6: Export models**

Modify `books_of_time/db/__init__.py` to include `CollectionRun` and `CollectionCoverageStat`.

- [ ] **Step 7: Verify GREEN**

Run:

```bash
uv run pytest tests/test_coverage_repositories.py -v
uv run ruff check books_of_time/coverage.py books_of_time/db/models.py books_of_time/db/repositories.py tests/test_coverage_repositories.py
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add books_of_time/coverage.py books_of_time/db/models.py books_of_time/db/repositories.py books_of_time/db/__init__.py tests/test_coverage_repositories.py
git commit -m "feat: add collection coverage repositories"
```

---

### Task 2: Worker Run And Failure Coverage

**Files:**
- Modify: `books_of_time/worker.py`
- Modify: `books_of_time/app.py`
- Test: `tests/test_worker_coverage.py`

**Interfaces:**
- Consumes: `CoverageDraft`.
- Consumes: `CollectionRunRepository` and `CollectionCoverageRepository`.
- Produces: `Worker(..., run_id: str, lease_owner: str, ...)`.
- Produces: `Collector.collect(...) -> CoverageDraft` protocol.

- [ ] **Step 1: Write failing worker tests**

Create `tests/test_worker_coverage.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.coverage import CoverageDraft
from books_of_time.db.base import Base
from books_of_time.db.models import CollectionCoverageStat, CollectionRun, CollectionTask
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind, TaskStatus
from books_of_time.worker import Worker


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class SuccessfulCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        return CoverageDraft(
            task_kind=task.kind,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=1,
            pages_succeeded=1,
            items_observed=1,
            raw_payloads_saved=1,
            reason="complete",
        )


class FailingCollector:
    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_worker_writes_run_and_coverage_on_success(session_factory) -> None:
    now = datetime(2099, 1, 1, tzinfo=UTC)
    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BV1xx",
            priority=100,
            payload={"bvid": "BV1xx"},
            not_before=now - timedelta(seconds=1),
        )
        await session.commit()

    worker = Worker(
        session_factory=session_factory,
        collectors={TaskKind.FETCH_VIDEO_STATS: SuccessfulCollector()},
        run_id="run-1",
        lease_owner="worker-1",
    )
    assert await worker.run_once(now=now) is True

    async with session_factory() as session:
        run = await session.scalar(select(CollectionRun))
        stat = await session.scalar(select(CollectionCoverageStat))
        task = await session.scalar(select(CollectionTask))
        assert run is not None
        assert run.tasks_started == 1
        assert run.tasks_succeeded == 1
        assert run.tasks_failed == 0
        assert stat is not None
        assert stat.run_id == "run-1"
        assert stat.status == "succeeded"
        assert stat.reason == "complete"
        assert task.status == TaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_worker_writes_failed_coverage_and_preserves_retry(
    session_factory,
) -> None:
    now = datetime(2099, 1, 1, tzinfo=UTC)
    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BV1xx",
            priority=100,
            payload={"bvid": "BV1xx"},
            not_before=now - timedelta(seconds=1),
            max_retries=3,
        )
        await session.commit()

    worker = Worker(
        session_factory=session_factory,
        collectors={TaskKind.FETCH_VIDEO_STATS: FailingCollector()},
        run_id="run-1",
        lease_owner="worker-1",
        retry_delay_seconds=30,
    )

    with pytest.raises(RuntimeError, match="boom"):
        await worker.run_once(now=now)

    async with session_factory() as session:
        run = await session.scalar(select(CollectionRun))
        stat = await session.scalar(select(CollectionCoverageStat))
        task = await session.scalar(select(CollectionTask))
        assert run is not None
        assert run.tasks_started == 1
        assert run.tasks_succeeded == 0
        assert run.tasks_failed == 1
        assert stat is not None
        assert stat.status == "failed"
        assert stat.reason == "collector_exception"
        assert stat.extra["exception_type"] == "RuntimeError"
        assert task.status == TaskStatus.PENDING
        assert task.retry_count == 1
        assert task.not_before == now + timedelta(seconds=30)
```

- [ ] **Step 2: Run tests to verify RED**

```bash
uv run pytest tests/test_worker_coverage.py -v
```

Expected: FAIL because `Worker` does not accept `run_id` and does not persist coverage.

- [ ] **Step 3: Widen worker protocol and persist coverage**

Modify `books_of_time/worker.py`:

```python
from books_of_time.coverage import CoverageDraft
from books_of_time.db.repositories import (
    CollectionCoverageRepository,
    CollectionRunRepository,
    CollectionTaskRepository,
)
```

Change the protocol:

```python
class Collector(Protocol):
    async def collect(
        self,
        task: CollectionTask,
        session: AsyncSession,
    ) -> CoverageDraft: ...
```

Add `run_id` to `Worker.__init__` and store it:

```python
self.run_id = run_id
```

Inside `run_once`, after leasing `task`, create repositories and record start:

```python
run_repo = CollectionRunRepository(session)
coverage_repo = CollectionCoverageRepository(session)
run = await run_repo.get_or_create_running(
    run_id=self.run_id,
    worker_id=self.lease_owner,
    now=effective_now,
)
await run_repo.record_task_started(run, now=effective_now)
```

On success:

```python
finished_at = datetime.now(UTC)
await coverage_repo.insert_from_draft(
    task=task,
    run_id=self.run_id,
    draft=draft,
    started_at=effective_now,
    finished_at=finished_at,
)
await run_repo.record_task_succeeded(run, now=finished_at)
```

On exception before re-raising:

```python
finished_at = datetime.now(UTC)
await coverage_repo.insert_failed(
    task=task,
    run_id=self.run_id,
    started_at=effective_now,
    finished_at=finished_at,
    reason="collector_exception",
    extra={"exception_type": type(exc).__name__, "message": str(exc)},
)
await run_repo.record_task_failed(run, now=finished_at)
```

Bind the exception as `except Exception as exc:`.

- [ ] **Step 4: Pass `run_id` from app**

Modify the `Worker(...)` construction in `books_of_time/app.py`:

```python
return Worker(
    session_factory=session_factory,
    collectors={...},
    run_id=run_id,
    lease_owner=lease_owner,
    ...
)
```

- [ ] **Step 5: Verify GREEN**

```bash
uv run pytest tests/test_worker_coverage.py tests/test_task_queue.py -v
uv run ruff check books_of_time/worker.py books_of_time/app.py tests/test_worker_coverage.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add books_of_time/worker.py books_of_time/app.py tests/test_worker_coverage.py
git commit -m "feat: record worker coverage outcomes"
```

---

### Task 3: Collector Coverage Drafts

**Files:**
- Modify: `books_of_time/collectors/video_stats.py`
- Modify: `books_of_time/collectors/hot_comments.py`
- Modify: `books_of_time/collectors/latest_comments.py`
- Modify: `tests/test_video_stats_worker.py`
- Modify: `tests/test_hot_comments_worker.py`
- Modify: `tests/test_latest_comments_worker.py`

**Interfaces:**
- Consumes: `CoverageDraft`.
- Produces: all existing collectors return `CoverageDraft`.

- [ ] **Step 1: Add expected coverage assertions to existing worker tests**

In `tests/test_video_stats_worker.py`, import `CollectionCoverageStat` and assert:

```python
coverage = await session.scalar(select(CollectionCoverageStat))
assert coverage is not None
assert coverage.task_kind == TaskKind.FETCH_VIDEO_STATS
assert coverage.status == "succeeded"
assert coverage.reason == "complete"
assert coverage.pages_requested == 1
assert coverage.pages_succeeded == 1
assert coverage.items_observed == 1
assert coverage.raw_payloads_saved == 1
```

In `tests/test_hot_comments_worker.py`, assert:

```python
coverage = await session.scalar(select(CollectionCoverageStat))
assert coverage is not None
assert coverage.task_kind == TaskKind.FETCH_HOT_COMMENTS
assert coverage.status == "succeeded"
assert coverage.reason == "complete"
assert coverage.pages_requested == 1
assert coverage.pages_succeeded == 1
assert coverage.items_observed == 1
assert coverage.raw_payloads_saved == 2
assert coverage.truncated is False
```

In `tests/test_latest_comments_worker.py`, add targeted assertions to these tests:

```python
coverage_rows = (
    await session.scalars(select(CollectionCoverageStat).order_by(CollectionCoverageStat.id.asc()))
).all()
assert coverage_rows[-1].status == "partial"
assert coverage_rows[-1].reason == "time_budget"
assert coverage_rows[-1].truncated is True
```

for the paused baseline case.

For frontier missing:

```python
assert coverage_rows[-1].status == "partial"
assert coverage_rows[-1].reason == "frontier_missing"
assert coverage_rows[-1].frontier_missing is True
assert coverage_rows[-1].frontier_reached is False
```

For corrupted:

```python
assert coverage_rows[-1].status == "corrupted"
assert coverage_rows[-1].reason == "page_retry_exhausted"
assert coverage_rows[-1].corrupted is True
```

- [ ] **Step 2: Run tests to verify RED**

```bash
uv run pytest tests/test_video_stats_worker.py tests/test_hot_comments_worker.py tests/test_latest_comments_worker.py -v
```

Expected: FAIL because collectors still return `None`.

- [ ] **Step 3: Return video stats coverage**

Modify `books_of_time/collectors/video_stats.py`:

```python
from books_of_time.coverage import CoverageDraft
from books_of_time.domain.enums import TaskKind
```

Change signature:

```python
async def collect(self, task: CollectionTask, session: AsyncSession) -> CoverageDraft:
```

Return after snapshot insert:

```python
return CoverageDraft(
    task_kind=TaskKind.FETCH_VIDEO_STATS,
    target_type=task.target_type,
    target_id=task.target_id,
    pages_requested=1,
    pages_succeeded=1,
    items_observed=1,
    raw_payloads_saved=1,
    reason="complete",
)
```

- [ ] **Step 4: Return hot comments coverage**

Modify `books_of_time/collectors/hot_comments.py` imports:

```python
from books_of_time.coverage import CoverageDraft
from books_of_time.domain.enums import BilibiliRequestType, TaskKind
```

Change signature:

```python
async def collect(self, task: CollectionTask, session: AsyncSession) -> CoverageDraft:
```

After `observations = await CommentRepository(...).upsert_page(...)`, return:

```python
return CoverageDraft(
    task_kind=TaskKind.FETCH_HOT_COMMENTS,
    target_type=task.target_type,
    target_id=task.target_id,
    pages_requested=1,
    pages_succeeded=1,
    items_observed=len(observations),
    raw_payloads_saved=2 if task.payload.get("video_raw_payload_id") else 1,
    truncated=False,
    reason="complete",
)
```

Use a local `raw_payloads_saved = 0` counter and increment after each `_archive_raw`
call so the value remains correct when `aid` is already present.

- [ ] **Step 5: Return latest comments coverage**

Modify `books_of_time/collectors/latest_comments.py` imports:

```python
from books_of_time.coverage import CoverageDraft
```

Change signature:

```python
async def collect(self, task: CollectionTask, session: AsyncSession) -> CoverageDraft:
```

At the end of `collect`, after mode dispatch, return the draft from
`_run_baseline_tail`, `_run_head_sweep`, or `_run_incremental`.

For each scan method, return a `CoverageDraft` with:

```python
CoverageDraft(
    task_kind=TaskKind.FETCH_LATEST_COMMENTS,
    target_type=task.target_type,
    target_id=task.target_id,
    pages_requested=state.last_scan_pages,
    pages_succeeded=state.last_scan_pages,
    items_observed=<comments written in this task>,
    raw_payloads_saved=state.last_scan_pages,
    frontier_reached=<true/false/none>,
    frontier_missing=<true/false/none>,
    truncated=state.last_scan_truncated,
    corrupted=state.last_scan_status == "corrupted",
    reason=<mapped reason>,
    extra={"baseline_status": state.extra.get("baseline_status")},
)
```

Track `items_observed` with a local counter incremented by the length returned
from `CommentRepository.upsert_page`. Track requested/succeeded pages at the
point each page fetch succeeds; for Phase 1C, failed page attempts can remain in
`request_errors` and `extra["failed_attempts"]`.

Use this reason mapping:

```python
if state.last_scan_status == "baseline_complete":
    reason = "baseline_complete"
elif state.last_scan_status == "frontier_reached":
    reason = "frontier_reached"
elif state.last_scan_status == "frontier_missing":
    reason = "frontier_missing"
elif state.last_scan_status == "paused":
    reason = "time_budget"
elif state.last_scan_status == "corrupted":
    reason = state.extra.get("failed_reason") or "page_retry_exhausted"
else:
    reason = state.last_scan_status or "complete"
```

- [ ] **Step 6: Verify GREEN**

```bash
uv run pytest tests/test_video_stats_worker.py tests/test_hot_comments_worker.py tests/test_latest_comments_worker.py tests/test_worker_coverage.py -v
uv run ruff check books_of_time/collectors/video_stats.py books_of_time/collectors/hot_comments.py books_of_time/collectors/latest_comments.py tests/test_video_stats_worker.py tests/test_hot_comments_worker.py tests/test_latest_comments_worker.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add books_of_time/collectors/video_stats.py books_of_time/collectors/hot_comments.py books_of_time/collectors/latest_comments.py tests/test_video_stats_worker.py tests/test_hot_comments_worker.py tests/test_latest_comments_worker.py
git commit -m "feat: record collector coverage drafts"
```

---

### Task 4: Coverage CLI

**Files:**
- Modify: `books_of_time/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `CollectionCoverageRepository.list_for_target(...)`.
- Produces: `bot coverage BVxxxx`.

- [ ] **Step 1: Add parser and query tests**

Modify `tests/test_cli.py`:

```python
def test_coverage_parser_accepts_bvid() -> None:
    args = build_parser().parse_args(["coverage", "BV1xx"])
    assert args.command == "coverage"
    assert args.bvid == "BV1xx"
```

Add an async query test with a temporary sqlite config:

```python
@pytest.mark.asyncio
async def test_print_coverage_lists_latest_rows(tmp_path, caplog) -> None:
    # Reuse the repository test setup pattern to create an in-memory DB, insert
    # one coverage row, then call the CLI helper directly.
```

Keep the helper direct-call test small. It only needs to prove `_show_coverage`
queries by BV id and logs status/reason/page counts.

- [ ] **Step 2: Run test to verify RED**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: FAIL because the parser lacks `coverage`.

- [ ] **Step 3: Add command and helper**

Modify `books_of_time/cli.py` imports:

```python
from books_of_time.db.repositories import (
    CollectionCoverageRepository,
    CollectionTaskRepository,
)
```

Add parser:

```python
coverage = subparsers.add_parser("coverage")
coverage.add_argument("bvid")
coverage.add_argument("--limit", type=int, default=20)
```

Add `_run` branch:

```python
if args.command == "coverage":
    await _show_coverage(cfg, args.bvid, args.limit)
    return
```

Add helper:

```python
async def _show_coverage(cfg: dict, bvid: str, limit: int) -> None:
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        rows = await CollectionCoverageRepository(session).list_for_target(
            target_type="video",
            target_id=bvid,
            limit=limit,
        )

    if not rows:
        logger.info("No coverage rows for %s", bvid)
        return

    for row in rows:
        logger.info(
            "%s %s status=%s reason=%s pages=%s/%s items=%s frontier_reached=%s "
            "frontier_missing=%s truncated=%s corrupted=%s",
            row.finished_at.isoformat(),
            row.task_kind,
            row.status,
            row.reason,
            row.pages_succeeded,
            row.pages_requested,
            row.items_observed,
            row.frontier_reached,
            row.frontier_missing,
            row.truncated,
            row.corrupted,
        )
```

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/test_cli.py tests/test_coverage_repositories.py -v
uv run ruff check books_of_time/cli.py tests/test_cli.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/cli.py tests/test_cli.py
git commit -m "feat: add coverage inspection cli"
```

---

### Task 5: Progress Doc And Full Verification

**Files:**
- Modify: `docs/TODO.md`
- Optional modify: `README.md` only if the CLI command needs a short usage note.

**Interfaces:**
- Consumes: all prior Phase 1C implementation.
- Produces: synced progress documentation and full verification evidence.

- [ ] **Step 1: Update progress checkboxes**

In `docs/TODO.md`, mark these `P1: Coverage And Data Quality` items complete:

```markdown
- [x] 建立 `collection_runs` 表。
- [x] 建立 `collection_coverage_stats` 表。
- [x] 记录 hot pages requested/succeeded。
- [x] 记录 latest pages requested/succeeded。
- [x] 记录 latest frontier reached。
- [x] 记录 request success rate。
- [x] 记录 parse error count。
- [x] CLI 支持 `bot coverage BVxxxx`。
- [x] 所有 collector 在成功或失败后都写覆盖率摘要。
```

Leave these unchecked unless separately implemented:

```markdown
- [ ] 记录 reply roots requested/succeeded。
```

If the implementation records request errors rather than a literal success-rate
float, add one clarifying sentence under the section:

```markdown
说明：Phase 1C 以 requested/succeeded/error 计数保存请求成功情况，查询层可由此计算 success rate。
```

- [ ] **Step 2: Run full verification**

```bash
uv run pytest
uv run ruff check .
```

Expected:

```text
43+ passed
All checks passed!
```

The exact pytest count may increase because this plan adds tests.

- [ ] **Step 3: Confirm only intended changes are staged**

```bash
git status --short --branch
git diff --stat -- docs/TODO.md README.md
```

Expected: only Phase 1C files and the pre-existing unrelated HTTP files are visible.

- [ ] **Step 4: Commit**

```bash
git add docs/TODO.md README.md
git commit -m "docs: mark coverage data quality progress"
```

If `README.md` was not changed, omit it from `git add`.

---

## Self-Review

Spec coverage:

- `collection_runs` table: Task 1.
- `collection_coverage_stats` table: Task 1.
- Worker persists coverage and run counters: Task 2.
- Video stats coverage: Task 3.
- Hot comments coverage: Task 3.
- Latest comments paused, complete, frontier missing, and corrupted coverage:
  Task 3.
- CLI inspection: Task 4.
- Progress doc and verification: Task 5.

Placeholder scan:

- The plan contains no `TBD`, no unspecified "add appropriate handling", and no
  task that says to write tests without concrete assertions.

Type consistency:

- `CoverageDraft.status` returns the text stored by
  `CollectionCoverageStat.status`.
- `CollectionCoverageRepository.insert_from_draft(...)` uses `CollectionTask`
  and `CoverageDraft` fields named in Task 1.
- `Worker` consumes the widened collector protocol used by all collectors in
  Task 3.

Execution choice:

Because the user asked to avoid aggressive subagent usage, execute this plan
inline in the main session unless the user explicitly asks to use subagents for
a later review checkpoint.
