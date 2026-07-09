# Phase 1E Worker Operations Implementation Plan

> **Execution mode:** Implement this plan inline in the main session. Avoid opening subagents unless the user explicitly asks for them again.

**Goal:** Make the existing collection task queue operable with a worker loop, queue inspection, failed-task retry, and expired lease recovery.

**Architecture:** Extend `CollectionTaskRepository` with queue operations, add a cooperative `Worker.run_loop()` around existing `run_once()`, and expose small CLI helpers for task inspection and retry. Keep behavior in repositories and worker methods so CLI stays thin and testable.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, argparse CLI, pytest-asyncio, Ruff.

## Global Constraints

- Keep the worker loop cooperative and simple. No multiprocessing, no daemon supervisor, no signal framework in this slice.
- The loop must be testable without waiting in real time.
- The loop must not swallow collector exceptions silently. `run_once()` keeps its current behavior of re-raising collector failures after committing retry state.
- CLI task list is inspection-only.
- `retry-failed` only moves `failed` tasks back to `pending`; it does not clone tasks or reset raw evidence.
- Lease recovery only handles tasks stuck in `running` with `lease_until <= now`.
- Task uniqueness/idempotency keys remain out of scope for Phase 1E.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and `books_of_time/http/rate_limiter.py`.
- Execute inline in the main session unless the user explicitly asks for subagents.

---

## File Structure

- Modify `books_of_time/db/repositories.py`: add `recover_expired_leases`, `list_tasks`, and `retry_failed`.
- Modify `books_of_time/worker.py`: add `run_loop`.
- Modify `books_of_time/cli.py`: add `worker loop`, `task list`, `task retry-failed`, and helper functions.
- Modify `tests/test_task_queue.py`: repository queue operation tests.
- Modify `tests/test_worker_coverage.py`: worker loop tests using lightweight collectors.
- Modify `tests/test_cli.py`: parser and helper tests.
- Modify `docs/TODO.md`: mark completed Task Queue items.

---

### Task 1: Queue Repository Operations

**Files:**
- Modify: `books_of_time/db/repositories.py`
- Modify: `tests/test_task_queue.py`

**Interfaces:**
- Produces: `CollectionTaskRepository.recover_expired_leases(now: datetime, limit: int = 100) -> int`
- Produces: `CollectionTaskRepository.list_tasks(status: TaskStatus | None = None, limit: int = 20) -> list[CollectionTask]`
- Produces: `CollectionTaskRepository.retry_failed(now: datetime, target_id: str | None = None, kind: TaskKind | None = None, limit: int = 100) -> int`

- [ ] **Step 1: Write failing repository tests**

Add tests to `tests/test_task_queue.py`:

```python
@pytest.mark.asyncio
async def test_task_repository_recovers_expired_running_leases() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2099, 1, 1, tzinfo=UTC)

    async with session_factory() as session:
        task = await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BV1xx",
            priority=10,
            payload={"bvid": "BV1xx"},
            not_before=now - timedelta(minutes=10),
        )
        task.status = TaskStatus.RUNNING
        task.lease_owner = "dead-worker"
        task.lease_until = now - timedelta(seconds=1)
        task.retry_count = 2
        await session.commit()

        recovered = await CollectionTaskRepository(session).recover_expired_leases(
            now=now
        )
        await session.commit()

        assert recovered == 1
        assert task.status == TaskStatus.PENDING
        assert task.lease_owner is None
        assert task.lease_until is None
        assert task.not_before == now
        assert task.retry_count == 2

    await engine.dispose()
```

Add list/retry-failed tests:

```python
@pytest.mark.asyncio
async def test_task_repository_lists_by_status_and_retries_failed() -> None:
    ...
    pending = await repo.list_tasks(status=TaskStatus.PENDING, limit=10)
    assert [task.target_id for task in pending] == ["BVPENDING"]
    retried = await repo.retry_failed(now=now, target_id="BVFAILED", limit=10)
    assert retried == 1
    assert failed.status == TaskStatus.PENDING
    assert failed.retry_count == 0
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_task_queue.py -v
```

Expected: FAIL because repository methods do not exist.

- [ ] **Step 3: Implement repository methods**

In `books_of_time/db/repositories.py`, add methods to `CollectionTaskRepository`:

```python
async def recover_expired_leases(self, *, now: datetime, limit: int = 100) -> int:
    rows = await self.session.scalars(
        select(CollectionTask)
        .where(
            CollectionTask.status == TaskStatus.RUNNING,
            CollectionTask.lease_until.is_not(None),
            CollectionTask.lease_until <= now,
        )
        .order_by(CollectionTask.lease_until.asc(), CollectionTask.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    tasks = list(rows)
    for task in tasks:
        task.status = TaskStatus.PENDING
        task.lease_owner = None
        task.lease_until = None
        task.not_before = now
    await self.session.flush()
    return len(tasks)
```

Add `list_tasks` and `retry_failed` with the interfaces above. `retry_failed`
filters `TaskStatus.FAILED`, optional `target_id`, optional `kind`, orders by
`updated_at.asc(), id.asc()`, resets status/lease/retry fields, and returns the
count.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/test_task_queue.py -v
uv run ruff check books_of_time/db/repositories.py tests/test_task_queue.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/db/repositories.py tests/test_task_queue.py
git commit -m "feat: add task queue operations"
```

---

### Task 2: Worker Loop

**Files:**
- Modify: `books_of_time/worker.py`
- Modify: `tests/test_worker_coverage.py`

**Interfaces:**
- Consumes: `CollectionTaskRepository.recover_expired_leases(...)`
- Produces: `Worker.run_loop(idle_sleep_seconds: float = 5, max_iterations: int | None = None, stop_when_idle: bool = False, sleep: Callable[[float], Awaitable[None]] | None = None) -> int`

- [ ] **Step 1: Write failing worker loop tests**

Add tests to `tests/test_worker_coverage.py`:

```python
class CountingCollector:
    def __init__(self) -> None:
        self.count = 0

    async def collect(self, task: CollectionTask, session) -> CoverageDraft:
        self.count += 1
        return CoverageDraft(
            task_kind=task.kind,
            target_type=task.target_type,
            target_id=task.target_id,
            reason="complete",
        )
```

Test two due tasks execute and loop stops when idle:

```python
executed = await worker.run_loop(
    idle_sleep_seconds=0,
    max_iterations=5,
    stop_when_idle=True,
    sleep=lambda seconds: None,
)
assert executed == 2
```

Test expired lease recovery before leasing.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_worker_coverage.py -v
```

Expected: FAIL because `run_loop` does not exist.

- [ ] **Step 3: Implement `run_loop`**

In `books_of_time/worker.py`, import:

```python
import asyncio
from collections.abc import Awaitable, Callable, Mapping
```

Add:

```python
async def run_loop(
    self,
    *,
    idle_sleep_seconds: float = 5,
    max_iterations: int | None = None,
    stop_when_idle: bool = False,
    sleep: Callable[[float], Awaitable[None] | None] | None = None,
) -> int:
    sleep_func = sleep or asyncio.sleep
    iterations = 0
    executed_count = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            recovered = await CollectionTaskRepository(session).recover_expired_leases(
                now=now
            )
            await session.commit()
        executed = await self.run_once(now=now)
        if executed:
            executed_count += 1
            continue
        if stop_when_idle:
            break
        maybe_awaitable = sleep_func(idle_sleep_seconds)
        if maybe_awaitable is not None:
            await maybe_awaitable
    return executed_count
```

The local `recovered` variable can be omitted if unused.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/test_worker_coverage.py tests/test_task_queue.py -v
uv run ruff check books_of_time/worker.py tests/test_worker_coverage.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/worker.py tests/test_worker_coverage.py
git commit -m "feat: add cooperative worker loop"
```

---

### Task 3: Task CLI And Progress Docs

**Files:**
- Modify: `books_of_time/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: queue repository methods.
- Produces: `bot worker loop`
- Produces: `bot task list`
- Produces: `bot task retry-failed`

- [ ] **Step 1: Write failing CLI tests**

In `tests/test_cli.py`, add parser tests:

```python
def test_worker_loop_parser_accepts_options() -> None:
    args = build_parser().parse_args(
        ["worker", "loop", "--idle-seconds", "0.1", "--max-iterations", "2"]
    )
    assert args.worker_command == "loop"
    assert args.idle_seconds == 0.1
    assert args.max_iterations == 2


def test_task_list_and_retry_failed_parsers() -> None:
    list_args = build_parser().parse_args(["task", "list", "--status", "failed"])
    assert list_args.command == "task"
    assert list_args.task_command == "list"
    retry_args = build_parser().parse_args(
        ["task", "retry-failed", "--target-id", "BV1xx", "--kind", "fetch_video_stats"]
    )
    assert retry_args.task_command == "retry-failed"
```

Add helper tests for `_list_tasks` and `_retry_failed_tasks` using a temporary
sqlite database, similar to existing coverage helper tests.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: FAIL because commands/helpers do not exist.

- [ ] **Step 3: Implement CLI**

In `build_parser()`:

- change `worker_sub.add_parser("run-once")` to also add `loop` with
  `--idle-seconds`, `--max-iterations`, and `--stop-when-idle`.
- add `task = subparsers.add_parser("task")`, with `list` and `retry-failed`.

In `_run()`:

- `worker run-once` keeps current behavior.
- `worker loop` builds worker and calls `run_loop(...)`.
- `task list` calls `_list_tasks(cfg, status, limit)`.
- `task retry-failed` calls `_retry_failed_tasks(cfg, target_id, kind, limit)`.

Add helpers:

```python
async def _list_tasks(cfg: dict, status: str | None, limit: int) -> None: ...
async def _retry_failed_tasks(
    cfg: dict,
    target_id: str | None,
    kind: str | None,
    limit: int,
) -> None: ...
```

Use `TaskStatus(status)` and `TaskKind(kind)` for validation.

- [ ] **Step 4: Update TODO**

Mark these in `docs/TODO.md`:

```markdown
- [x] 增加 `worker loop`。
- [x] 增加 `task list` CLI。
- [x] 增加 `task retry-failed` CLI。
- [x] 增加 running task lease 过期回收。
- [x] 增加 collection run id 与 run 生命周期表。
```

Leave task uniqueness/idempotency unchecked.

- [ ] **Step 5: Full verification**

```bash
uv run pytest
uv run ruff check .
```

Expected:

```text
61+ passed
All checks passed!
```

- [ ] **Step 6: Commit**

```bash
git add books_of_time/cli.py tests/test_cli.py docs/TODO.md
git commit -m "feat: add task queue cli operations"
```

---

## Self-Review

Spec coverage:

- Worker loop: Task 2.
- `bot worker loop`: Task 3.
- `bot task list`: Task 3.
- `bot task retry-failed`: Task 3.
- Expired lease recovery: Task 1 and Task 2.
- TODO sync: Task 3.

Placeholder scan:

- No `TBD`, no unspecified "appropriate" handling, and every task includes a
  concrete test command.

Type consistency:

- CLI status uses `TaskStatus` values.
- CLI kind uses `TaskKind` values.
- Repository methods return concrete counts/lists consumed by worker and CLI.

Execution choice:

Execute inline in the main session. Do not dispatch subagents unless the user
explicitly asks for them again.
