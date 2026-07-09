# Phase 1E Worker Operations Design

## Context

The project now has a PostgreSQL-backed task queue, `worker run-once`, run and
coverage audit rows, typed request backoff, and collectors for video stats,
hot comments, and latest comments. The queue is still awkward to operate:

- there is no long-running worker loop
- there is no task list CLI
- failed tasks cannot be re-queued from CLI
- running tasks whose leases expired are not recovered

Phase 1E makes the existing task queue operable without adding a scheduler or
new collection domains.

## Goal

Provide basic worker operations for unattended collection and manual queue
inspection.

The system must support:

1. A testable worker loop that repeatedly runs due tasks and sleeps when idle.
2. CLI command `bot worker loop`.
3. CLI command `bot task list`.
4. CLI command `bot task retry-failed`.
5. Repository support for recovering expired running leases.

## Approved Design Constraints

- Keep the worker loop cooperative and simple. No multiprocessing, no daemon
  supervisor, no signal framework in this slice.
- The loop must be testable without waiting in real time.
- The loop must not swallow collector exceptions silently. `run_once()` keeps
  its current behavior of re-raising collector failures after committing retry
  state.
- CLI task list is inspection-only.
- `retry-failed` only moves `failed` tasks back to `pending`; it does not clone
  tasks or reset raw evidence.
- Lease recovery only handles tasks stuck in `running` with
  `lease_until <= now`.
- Task uniqueness/idempotency keys remain out of scope for Phase 1E.

## Worker Loop

Add:

```python
async def run_loop(
    self,
    *,
    idle_sleep_seconds: float = 5,
    max_iterations: int | None = None,
    stop_when_idle: bool = False,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> int:
    ...
```

Semantics:

- Each iteration first recovers expired running leases.
- Then it calls `run_once()`.
- If a task executed, the loop immediately continues.
- If no task executed:
  - increment idle iteration count
  - if `stop_when_idle` is true, stop
  - otherwise sleep `idle_sleep_seconds`
- `max_iterations` limits loop iterations for tests and CLI smoke runs.
- Return the number of tasks successfully executed by `run_once()`.

If `run_once()` raises, `run_loop()` propagates the exception. The task state is
already committed by `run_once()`.

## Lease Recovery

Add repository method:

```python
async def recover_expired_leases(
    *,
    now: datetime,
    limit: int = 100,
) -> int:
    ...
```

For each task with:

- `status == running`
- `lease_until is not None`
- `lease_until <= now`

set:

- `status = pending`
- `lease_owner = None`
- `lease_until = None`
- `not_before = now`

Do not increment `retry_count`. Lease expiry means the worker disappeared; it is
not evidence that the task itself failed.

## Task List CLI

Add:

```text
bot task list --status pending --limit 20
```

Options:

- `--status`: optional task status filter.
- `--limit`: default `20`, capped at `200`.

Output one log line per task with:

- id
- kind
- target
- status
- priority
- retry count/max retries
- not_before
- lease owner and lease_until when present

## Retry Failed CLI

Add:

```text
bot task retry-failed --target-id BVxxxx --kind fetch_latest_comments --limit 20
```

Options:

- `--target-id`: optional filter.
- `--kind`: optional `TaskKind` value.
- `--limit`: default `100`, capped at `500`.

Semantics:

- Select failed tasks matching filters, oldest first.
- Set status to `pending`.
- Set `not_before = now`.
- Set `lease_owner = None`, `lease_until = None`.
- Set `retry_count = 0`.
- Return/log count retried.

## Collection Run TODO Sync

Phase 1C already added `collection_runs` and worker run counters. Phase 1E will
mark the Task Queue TODO item `增加 collection run id 与 run 生命周期表` as complete
after full verification, because worker operations now depend on that run
identity.

## Testing

Required tests:

1. Repository recovers expired running leases without changing retry count.
2. Repository task list filters by status and respects limit.
3. Repository retry-failed requeues failed tasks and resets retry count.
4. Worker loop executes multiple due tasks and stops when idle in tests.
5. Worker loop recovers expired leases before leasing the next task.
6. CLI parser accepts `worker loop`, `task list`, and `task retry-failed`.
7. CLI helper tests prove task list and retry-failed query/update the database.

## Acceptance Criteria

- `Worker.run_loop()` exists and is covered by tests.
- `bot worker loop` exists with finite test-friendly options.
- `bot task list` exists.
- `bot task retry-failed` exists.
- Expired running leases can be recovered.
- Task Queue TODO checkboxes for worker loop, task list, retry-failed, lease
  recovery, and collection run lifecycle are updated.
- `uv run pytest` and `uv run ruff check .` pass.

## Out Of Scope

- Task uniqueness/idempotency keys.
- Discovery loop.
- Scheduler policy.
- OS service management.
- Parallel worker process orchestration.
