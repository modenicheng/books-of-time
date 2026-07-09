# Phase 1C Coverage And Data Quality Design

## Context

Phase 1A added hot comment collection and append-only comment observations.
Phase 1B added latest-comment baseline and incremental frontier scans. Both
collectors preserve raw payloads and page observations, but there is no durable
run-level or task-level coverage summary yet.

The roadmap requires every report to show coverage, failed windows, and
uncertainty. Phase 1C builds the minimum data-quality layer needed for that
requirement without starting event-level analytics.

## Goal

Record what each collection task attempted, what it successfully captured, and
why the result should or should not be trusted as complete for that task.

The system must support:

1. A durable `collection_runs` table for worker-run audit context.
2. A durable `collection_coverage_stats` table with one summary row per
   collection task execution.
3. Coverage rows for video stats, hot comments, and latest comments.
4. CLI inspection for one video's latest coverage state.
5. Explicit success, partial, failed, and corrupted coverage outcomes.

## Approved Design Constraints

- Keep coverage factual. Do not infer platform truth beyond observed responses.
- Do not mark missing comments as deleted merely because a frontier or page was
  not reached.
- Preserve existing raw evidence flow. Coverage summarizes observations; it does
  not replace `raw_payloads`, `raw_page_observations`, or comment observations.
- The primary acceptance unit is one `collection_task` execution. A
  `collection_run` groups worker execution context but is not the only way to
  query coverage.
- Phase 1C does not add event-level coverage aggregation. Event coverage remains
  a later Phase 2 slice.
- Phase 1C does not implement request error taxonomy or global request backoff
  tables. It records collector-visible failure categories as strings until that
  lower layer exists.

## Data Model

### `collection_runs`

`collection_runs` records worker execution context. The initial model is
lightweight:

- `id`: big integer primary key.
- `run_id`: unique text id already created by `build_worker()`.
- `worker_id`: text owner, usually the worker lease owner.
- `started_at`: UTC datetime.
- `finished_at`: nullable UTC datetime.
- `status`: text enum-like value: `running`, `succeeded`, `failed`.
- `tasks_started`: integer count.
- `tasks_succeeded`: integer count.
- `tasks_failed`: integer count.
- `extra`: JSON object for future diagnostics.
- `created_at`, `updated_at`: UTC datetimes.

The first implementation creates one run row lazily when a worker instance
executes its first task. A later long-running worker loop can reuse the same
repository methods.

### `collection_coverage_stats`

`collection_coverage_stats` records one factual summary per task execution:

- `id`: big integer primary key.
- `collection_task_id`: task id.
- `run_id`: text id, matching `collection_runs.run_id`.
- `task_kind`: task kind value.
- `target_type`: text, initially `video`.
- `target_id`: text, usually BV id.
- `started_at`: UTC datetime.
- `finished_at`: UTC datetime.
- `status`: `succeeded`, `partial`, `failed`, or `corrupted`.
- `pages_requested`: integer.
- `pages_succeeded`: integer.
- `items_observed`: integer.
- `raw_payloads_saved`: integer.
- `parse_errors`: integer.
- `request_errors`: integer.
- `frontier_reached`: nullable boolean.
- `frontier_missing`: nullable boolean.
- `truncated`: boolean.
- `corrupted`: boolean.
- `reason`: nullable text, such as `complete`, `time_budget`, `frontier_missing`,
  `page_retry_exhausted`, `collector_exception`, or `parse_error`.
- `extra`: JSON object for collector-specific fields.
- `created_at`, `updated_at`: UTC datetimes.

Indexes:

- `(target_type, target_id, finished_at DESC)` for `bot coverage BVxxxx`.
- `(collection_task_id)` for debugging a task.
- `(run_id)` for run audit.

## Collector Coverage Semantics

### Video Stats

Video stats collection has no pagination.

- `pages_requested=1`
- `pages_succeeded=1` when raw payload and parsed snapshot are saved.
- `items_observed=1` when a snapshot row is written.
- `status=succeeded`, `reason=complete` on success.
- On collector exception, worker records `status=failed`,
  `reason=collector_exception`, and best-effort counters.

### Hot Comments

Current hot comment collection requests one hot page.

- `pages_requested=1`
- `pages_succeeded=1` when a raw page observation is written.
- `items_observed=len(parsed.comments)`
- `frontier_reached=null`
- `truncated=false` for the current one-page collector because configured
  deeper hot pages are not part of Phase 1C.

When configurable hot page depth is implemented later, it will update these
counters without changing the table shape.

### Latest Comments

Latest-comment coverage uses the Phase 1B frontier state and page counters:

- Baseline tail or head sweep paused by time budget:
  - `status=partial`
  - `truncated=true`
  - `reason=time_budget`
  - `frontier_reached=false`
- Baseline complete after head sweep:
  - `status=succeeded`
  - `truncated=false`
  - `reason=baseline_complete`
  - `frontier_reached=true`
- Incremental scan reaches previous frontier:
  - `status=succeeded`
  - `truncated=false`
  - `reason=frontier_reached`
  - `frontier_reached=true`
- Incremental scan reaches service end without seeing previous frontier:
  - `status=partial`
  - `truncated=false`
  - `reason=frontier_missing`
  - `frontier_reached=false`
  - `frontier_missing=true`
- Repeated cursor failure or cursor loop:
  - `status=corrupted`
  - `truncated=true`
  - `reason=page_retry_exhausted` or `cursor_loop`

## Worker Responsibilities

The worker owns task lifecycle and therefore owns persistence of coverage rows.
Collectors own domain counters and return a coverage draft on normal
completion. This keeps the worker transaction as the single place that marks a
task succeeded, retried, failed, and covered.

Phase 1C uses a small service object:

```python
@dataclass
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
```

Phase 1C widens the collector protocol from:

```python
async def collect(self, task: CollectionTask, session: AsyncSession) -> None: ...
```

to:

```python
async def collect(
    self,
    task: CollectionTask,
    session: AsyncSession,
) -> CoverageDraft: ...
```

The worker receives the draft, writes one `collection_coverage_stats` row,
marks the task succeeded, updates the current `collection_runs` counters, and
commits. If the collector raises before returning a draft, the worker writes a
best-effort failed coverage row with `reason=collector_exception`, then keeps
the existing retry/failure behavior.

## CLI

Add:

```text
bot coverage BVxxxx
```

The command prints the latest coverage rows for the target video, newest first.
It should include:

- task kind
- finished time
- status and reason
- pages requested/succeeded
- items observed
- frontier reached/missing when known
- truncated/corrupted flags

This is an inspection command only. It must not trigger collection.

## Testing

Required tests:

1. Repository tests create `collection_runs` and `collection_coverage_stats`.
2. Video stats worker success writes one succeeded coverage row.
3. Hot comments worker success writes one succeeded coverage row.
4. Latest comments paused baseline writes one partial coverage row with
   `reason=time_budget`.
5. Latest comments frontier missing writes one partial coverage row with
   `reason=frontier_missing`.
6. Latest comments corrupted scan writes one corrupted coverage row.
7. Worker collector exception writes one failed coverage row and still preserves
   existing task retry behavior.
8. `bot coverage BVxxxx` parser and query path are covered without requiring a
   real Bilibili request.

## Acceptance Criteria

- `collection_runs` and `collection_coverage_stats` are represented in ORM and
  repositories.
- Every currently implemented collector produces one coverage row per task
  execution.
- Worker-level failures produce failed coverage without hiding retry behavior.
- CLI can list latest coverage for one BV id.
- `docs/TODO.md` marks the Phase 1C coverage items that are completed by this
  slice.
- `uv run pytest` and `uv run ruff check .` pass.

## Out Of Scope

- Event-level coverage aggregation.
- Request-layer error taxonomy tables.
- Global request backoff state.
- UI dashboards.
- Derived conclusions about deleted or hidden comments.
