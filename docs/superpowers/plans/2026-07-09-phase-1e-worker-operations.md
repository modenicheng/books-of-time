# Phase 1E Worker Operations Implementation Plan

> **Execution mode:** Implement inline in this main session. Avoid opening subagents unless the user explicitly asks for them again.

**Goal:** Make the existing task queue operable through a long-running worker loop, task inspection CLI, failed-task retry CLI, and expired lease recovery.

**Architecture:** Extend `CollectionTaskRepository` with small queue operations, then have `Worker` recover expired leases before each lease attempt and expose a cooperative `run_loop()`. Add CLI commands that call those repository and worker APIs directly.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, argparse CLI, pytest-asyncio, Ruff.

## Global Constraints

- Keep the worker loop cooperative and simple. No multiprocessing, daemon supervisor, or signal framework in this slice.
- The loop must be testable without waiting in real time.
- `run_once()` keeps re-raising collector failures after committing retry state.
- `task list` is inspection-only.
- `retry-failed` only moves `failed` tasks back to `pending`; it does not clone tasks or reset raw evidence.
- Lease recovery only handles tasks stuck in `running` with `lease_until <= now`.
- Do not increment `retry_count` during lease recovery.
- Task uniqueness/idempotency keys remain out of scope for Phase 1E.
- Execute inline in this main session; do not dispatch subagents unless the user asks again.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and `books_of_time/http/rate_limiter.py`.

---

## File Structure

- Modify `books_of_time/db/repositories.py`: add task listing, failed retry, and expired lease recovery methods.
- Modify `books_of_time/worker.py`: add `run_loop()` and call lease recovery before leasing a task.
- Modify `books_of_time/cli.py`: add `worker loop`, `task list`, and `task retry-failed` commands.
- Modify `docs/TODO.md`: mark completed Phase 1E Task Queue items.
- Modify `tests/test_task_queue.py`: repository queue operation tests.
- Modify `tests/test_worker_loop.py`: worker loop behavior tests.
- Modify `tests/test_cli.py`: parser and CLI helper tests.

---

### Task 1: Repository Queue Operations

**Files:**
- Modify: `books_of_time/db/repositories.py`
- Test: `tests/test_task_queue.py`

**Interfaces:**
- Produces: `CollectionTaskRepository.list_tasks(status: TaskStatus | None = None, limit: int = 20) -> list[CollectionTask]`.
- Produces: `CollectionTaskRepository.retry_failed(target_id: str | None, kind: TaskKind | None, now: datetime, limit: int = 100) -> int`.
- Produces: `CollectionTaskRepository.recover_expired_leases(now: datetime, limit: int = 100) -> int`.

- [ ] **Step 1: Write failing repository tests**

Add tests that enqueue tasks, manually set statuses, and assert:

```python
async def test_task_repository_lists_by_status_and_limit(db_session):
    repo = CollectionTaskRepository(db_session)
    await repo.enqueue(
        kind=TaskKind.FETCH_VIDEO_STATS,
        target_type="video",
        target_id="BV1",
        priority=100,
        payload={"bvid": "BV1"},
        not_before=datetime(2099, 1, 1, tzinfo=UTC),
    )
    second = await repo.enqueue(
        kind=TaskKind.FETCH_HOT_COMMENTS,
        target_type="video",
        target_id="BV2",
        priority=90,
        payload={"bvid": "BV2"},
        not_before=datetime(2099, 1, 1, tzinfo=UTC),
    )
    second.status = TaskStatus.FAILED
    await db_session.commit()

    failed = await repo.list_tasks(status=TaskStatus.FAILED, limit=10)

    assert [task.target_id for task in failed] == ["BV2"]
```

```python
async def test_task_repository_retries_failed_tasks(db_session):
    repo = CollectionTaskRepository(db_session)
    task = await repo.enqueue(
        kind=TaskKind.FETCH_LATEST_COMMENTS,
        target_type="video",
        target_id="BV3",
        priority=70,
        payload={"bvid": "BV3"},
        not_before=datetime(2099, 1, 1, tzinfo=UTC),
    )
    task.status = TaskStatus.FAILED
    task.retry_count = 3
    task.lease_owner = "dead-worker"
    task.lease_until = datetime(2099, 1, 1, tzinfo=UTC)
    await db_session.commit()

    retried = await repo.retry_failed(
        target_id="BV3",
        kind=TaskKind.FETCH_LATEST_COMMENTS,
        now=datetime(2099, 1, 2, tzinfo=UTC),
        limit=100,
    )
    await db_session.refresh(task)

    assert retried == 1
    assert task.status == TaskStatus.PENDING
    assert task.retry_count == 0
    assert task.lease_owner is None
    assert task.lease_until is None
```

```python
async def test_task_repository_recovers_expired_running_leases(db_session):
    repo = CollectionTaskRepository(db_session)
    task = await repo.enqueue(
        kind=TaskKind.FETCH_VIDEO_STATS,
        target_type="video",
        target_id="BV4",
        priority=100,
        payload={"bvid": "BV4"},
        not_before=datetime(2099, 1, 1, tzinfo=UTC),
    )
    task.status = TaskStatus.RUNNING
    task.retry_count = 2
    task.lease_owner = "dead-worker"
    task.lease_until = datetime(2099, 1, 1, tzinfo=UTC)
    await db_session.commit()

    recovered = await repo.recover_expired_leases(
        now=datetime(2099, 1, 1, 0, 0, 1, tzinfo=UTC),
        limit=100,
    )
    await db_session.refresh(task)

    assert recovered == 1
    assert task.status == TaskStatus.PENDING
    assert task.retry_count == 2
    assert task.not_before == datetime(2099, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert task.lease_owner is None
    assert task.lease_until is None
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_task_queue.py -v
```

Expected: FAIL because the repository methods do not exist.

- [ ] **Step 3: Implement repository methods**

Use SQLAlchemy `select(CollectionTask)`, order by priority and id for list output, filter failed tasks oldest first for retry, and update matching expired running leases in ORM objects to keep behavior easy to inspect.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_task_queue.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/db/repositories.py tests/test_task_queue.py
git commit -m "feat: add task queue operations"
```

---

### Task 2: Worker Loop And Lease Recovery

**Files:**
- Modify: `books_of_time/worker.py`
- Test: `tests/test_worker_loop.py`

**Interfaces:**
- Consumes: `CollectionTaskRepository.recover_expired_leases(now, limit)`.
- Produces: `Worker.run_loop(idle_sleep_seconds: float = 5, max_iterations: int | None = None, stop_when_idle: bool = False, sleep: Callable[[float], Awaitable[None]] | None = None) -> int`.

- [ ] **Step 1: Write failing worker loop tests**

Add tests that use a simple collector returning `CoverageDraft` and assert:

```python
async def test_worker_loop_runs_due_tasks_until_idle(session_factory):
    slept = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    worker = Worker(
        session_factory=session_factory,
        collectors={TaskKind.FETCH_VIDEO_STATS: collector},
        run_id="run-loop-test",
        lease_owner="worker-1",
    )

    executed = await worker.run_loop(
        idle_sleep_seconds=0.5,
        max_iterations=3,
        sleep=fake_sleep,
    )

    assert executed == 2
    assert slept == [0.5]
```

```python
async def test_worker_recovers_expired_lease_before_leasing(session_factory):
    async with session_factory() as session:
        task = await CollectionTaskRepository(session).enqueue(...)
        task.status = TaskStatus.RUNNING
        task.lease_owner = "dead-worker"
        task.lease_until = datetime(2099, 1, 1, tzinfo=UTC)
        await session.commit()

    worker = Worker(...)
    executed = await worker.run_once(
        now=datetime(2099, 1, 1, 0, 0, 1, tzinfo=UTC),
    )

    assert executed is True
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_worker_loop.py -v
```

Expected: FAIL because `run_loop()` and recovery integration do not exist.

- [ ] **Step 3: Implement worker behavior**

Add `recover_expired_leases()` at the start of `run_once()`. Add `run_loop()` using injectable `sleep` and `max_iterations` for tests. Propagate exceptions from `run_once()`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_worker_loop.py tests/test_worker_coverage.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/worker.py tests/test_worker_loop.py
git commit -m "feat: add worker loop"
```

---

### Task 3: CLI Worker And Task Operations

**Files:**
- Modify: `books_of_time/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Worker.run_loop(...)`.
- Consumes: `CollectionTaskRepository.list_tasks(...)`.
- Consumes: `CollectionTaskRepository.retry_failed(...)`.

- [ ] **Step 1: Write failing CLI tests**

Add parser tests:

```python
def test_parser_accepts_worker_loop():
    args = build_parser().parse_args(["worker", "loop", "--max-iterations", "1"])

    assert args.command == "worker"
    assert args.worker_command == "loop"
    assert args.max_iterations == 1
```

```python
def test_parser_accepts_task_list_and_retry_failed():
    list_args = build_parser().parse_args(["task", "list", "--status", "failed"])
    retry_args = build_parser().parse_args(
        [
            "task",
            "retry-failed",
            "--target-id",
            "BV1",
            "--kind",
            "fetch_latest_comments",
        ]
    )

    assert list_args.task_command == "list"
    assert list_args.status == "failed"
    assert retry_args.task_command == "retry-failed"
    assert retry_args.target_id == "BV1"
    assert retry_args.kind == "fetch_latest_comments"
```

Add helper tests for `_list_tasks()` and `_retry_failed_tasks()` using the test DB.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_cli.py -v
```

Expected: FAIL because commands and helpers do not exist.

- [ ] **Step 3: Implement CLI**

Add:

- `bot worker loop --idle-sleep-seconds 5 --max-iterations N --stop-when-idle`
- `bot task list --status STATUS --limit LIMIT`
- `bot task retry-failed --target-id TARGET --kind KIND --limit LIMIT`

Clamp list limits to `200` and retry limits to `500`. Parse `TaskStatus` and `TaskKind` from enum values.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_cli.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/cli.py tests/test_cli.py
git commit -m "feat: add task operations cli"
```

---

### Task 4: TODO Sync And Full Verification

**Files:**
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: completed repository, worker, and CLI behavior.

- [ ] **Step 1: Update TODO checkboxes**

Mark these items complete:

- `增加 worker loop。`
- `增加 task list CLI。`
- `增加 task retry-failed CLI。`
- `增加 running task lease 过期回收。`
- `增加 collection run id 与 run 生命周期表。`

Leave task uniqueness/idempotency unchecked.

- [ ] **Step 2: Run full tests**

```bash
uv run pytest
```

Expected: all tests pass.

- [ ] **Step 3: Run lint**

```bash
uv run ruff check .
```

Expected: exit code 0.

- [ ] **Step 4: Commit**

```bash
git add docs/TODO.md
git commit -m "docs: mark worker operations progress"
```

---

## Self-Review

- Spec coverage: the plan covers worker loop, CLI worker loop, task list, retry-failed, expired lease recovery, TODO sync, and verification.
- Placeholder scan: no TBD/fill-later placeholders are present.
- Type consistency: repository, worker, and CLI method names match the Phase 1E design.
