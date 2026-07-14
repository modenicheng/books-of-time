# Hot Core And Deep Scans C4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement tiered routine and checkpoint hot-comment collection as persistent, restart-safe `hot_core` and `hot_deep` scans with 55-second numbered slices and all-status slice idempotency.

**Architecture:** Extend the versioned cohort policy with immutable hot-page targets, introduce `comment_scan_runs` as the authoritative page-number scan state, and attach numbered slice identity directly to collection tasks. The C3 planner persists exact hot component page ranges in shadow mode; lower-level live materialization creates one scan run and slice zero atomically. `HotCommentCollector` advances the scan and enqueues the next slice in the worker transaction, while component lifecycle reads the scan run so a yielded slice cannot prematurely finish its cohort.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async ORM, Alembic, PostgreSQL, SQLite test cycles, pytest, Ruff, existing Bilibili client/raw/comment/media repositories.

## Global Constraints

- C3 service rollout remains `shadow` only. C4 must not enable live cohort ownership or retire the legacy sweep; that transfer remains C7.
- Active routine hot targets are S/A/B/C = 3/2/1/1 pages.
- Checkpoint and first active adoption hot totals are S/A/B/C = 20/10/3/1 pages; `hot_core` pages count toward that total and `hot_deep` owns only the remainder.
- Dormant routine hot collection remains one page; archived videos remain metric-only.
- One numbered hot task slice may request at most 10 pages and may run for at most 55 seconds.
- An explicit platform end marker or an empty successful hot page ends the scan before its configured target.
- `scan_slice_key=(comment_scan_run_id, phase, slice_no)` is nullable but unique across every task status. The active-only `idempotency_key` remains for ordinary tasks.
- Scan progress and next-slice insertion commit in the same worker transaction.
- Every page keeps its own HTTP/raw/parser capture time. A multi-page scan is never described as a frozen server-side transaction.
- Public user identifiers remain visible for validation. Media remains local filesystem storage.
- Docker continues to connect to external PostgreSQL; Linux native, Windows native, and Docker use the same schema and behavior.
- Never downgrade the configured PostgreSQL database. Migration downgrade verification uses disposable SQLite databases or an isolated disposable PostgreSQL schema.
- Use TDD for every behavior change and one Conventional Commit per task.

---

### Task 1: Versioned Hot Comment Policy And Page Plans

**Files:**
- Modify: `books_of_time/domain/cohort_policy.py`
- Modify: `config/config.yaml.example`
- Modify: `tests/test_cohort_policy_config.py`
- Create: `tests/test_hot_scan_policy.py`

**Interfaces:**
- Produces `HotCommentPolicy` with `routine_pages`, `checkpoint_pages`, `max_pages_per_slice`, and `max_slice_seconds`.
- Produces `HotPagePlan(core_start_page, core_pages, deep_start_page, deep_pages, total_pages)`.
- Produces `hot_page_plan(policy, tier, *, include_deep, dormant=False) -> HotPagePlan`.
- Extends `CohortPolicy.hot_comments` and persists the normalized values in `as_persisted_policy()`.

- [x] **Step 1: Write failing policy parsing and page-plan tests**

Add tests that assert:

```python
policy = CohortPolicy.from_config(
    {
        "scheduler": {"lease_seconds": 120},
        "snapshot_cohorts": {
            "policy_version": "cohort-default-v2",
            "hot_comments": {
                "routine_pages": {"s": 3, "a": 2, "b": 1, "c": 1},
                "checkpoint_pages": {"s": 20, "a": 10, "b": 3, "c": 1},
                "max_pages_per_slice": 10,
                "max_slice_seconds": 55,
            },
        },
    }
)

assert hot_page_plan(
    policy, CollectionTier.S, include_deep=True
) == HotPagePlan(1, 3, 4, 17, 20)
assert hot_page_plan(
    policy, CollectionTier.A, include_deep=False
) == HotPagePlan(1, 2, 3, 0, 2)
assert hot_page_plan(
    policy, CollectionTier.B, include_deep=True
) == HotPagePlan(1, 1, 2, 2, 3)
assert hot_page_plan(
    policy, CollectionTier.C, include_deep=True
) == HotPagePlan(1, 1, 2, 0, 1)
assert hot_page_plan(
    policy, CollectionTier.S, include_deep=True, dormant=True
) == HotPagePlan(1, 1, 2, 0, 1)
```

Also assert that policy persistence contains the full hot section and that omitted configuration receives the values above with default `policy_version == "cohort-default-v2"`.

- [x] **Step 2: Run the new tests and verify RED**

```powershell
uv run pytest tests/test_hot_scan_policy.py tests/test_cohort_policy_config.py -q
```

Expected: imports or attributes for `HotCommentPolicy`, `HotPagePlan`, `hot_page_plan`, and `CohortPolicy.hot_comments` fail.

- [x] **Step 3: Implement immutable policy parsing and validation**

Add frozen dataclasses and strict parsing. Validation must reject:

```text
unknown tier keys
boolean or non-positive page counts
checkpoint page count lower than routine page count
non-positive max_pages_per_slice
non-positive max_slice_seconds
max_slice_seconds >= scheduler.lease_seconds
```

Normalize mappings to `MappingProxyType`, include all values in `as_persisted_policy()`, and bump the repository template/default policy version from `cohort-default-v1` to `cohort-default-v2`. Existing explicitly configured v1 remains readable, but operators must choose v2 when enabling the changed content.

- [x] **Step 4: Run focused policy tests**

```powershell
uv run pytest tests/test_hot_scan_policy.py tests/test_cohort_policy_config.py tests/test_cohort_time_policy.py -q
uv run ruff check books_of_time/domain/cohort_policy.py tests/test_hot_scan_policy.py tests/test_cohort_policy_config.py
```

Expected: all tests pass and Ruff is clean.

- [x] **Step 5: Commit the policy unit**

```powershell
git add books_of_time/domain/cohort_policy.py config/config.yaml.example tests/test_hot_scan_policy.py tests/test_cohort_policy_config.py docs/superpowers/plans/2026-07-14-hot-core-deep-scans-c4.md
git commit -m "feat(policy): add tiered hot scan targets"
```

---

### Task 2: Persistent Comment Scan Schema And Evidence Links

**Files:**
- Create: `alembic/versions/0011_hot_comment_scans.py`
- Modify: `books_of_time/db/models.py`
- Modify: `books_of_time/db/migrations.py`
- Modify: `books_of_time/domain/enums.py`
- Modify: `tests/test_schema_migrations.py`
- Create: `tests/test_comment_scan_models.py`

**Interfaces:**
- Produces `CommentScanMode` values `hot_core`, `hot_deep`, `baseline_tail`, `baseline_head_sweep`, `incremental`, `full_reconciliation`, `segmented_reconciliation`, `reply_refresh`, and `visibility_probe`.
- Produces `CommentScanStatus` values `planned`, `running`, `paused`, `complete`, `partial`, `failed`, and `corrupted`.
- Produces ORM model `CommentScanRun` and schema revision `0011_hot_comment_scans`.
- Extends `CollectionTask` with nullable `comment_scan_run_id`, `scan_slice_no`, and globally unique `scan_slice_key`.
- Extends `CollectionCoverageStat.comment_scan_run_id`, `RawPageObservation.scan_run_id`, and `CommentObservation.scan_run_id`.

- [x] **Step 1: Write failing model contract tests**

Assert the model exposes the scan-run fields from design section 6.6:

```text
id, scan_key, bvid, oid, snapshot_cohort_id, parent_scan_run_id,
mode, status, outcome, started_at, finished_at,
start_frontier_rpid, result_frontier_rpid,
start_anchor_set, result_anchor_set,
start_cursor, result_cursor,
target_pages, next_page_number,
pages_requested, pages_succeeded, items_observed, raw_payloads_saved,
slice_count, truncated, last_error_type, last_error_message,
reason, policy_version, extra, created_at, updated_at
```

Use an in-memory SQLite database to assert one run persists, task/evidence rows accept its ID, duplicate non-NULL `scan_key` fails, and duplicate non-NULL `scan_slice_key` fails even when the first task is `succeeded`.

- [x] **Step 2: Run model tests and verify RED**

```powershell
uv run pytest tests/test_comment_scan_models.py -q
```

Expected: `CommentScanRun`, scan enums, and new columns do not exist.

- [x] **Step 3: Implement ORM models and indexes**

Use the existing UTC/JSON/bigint helpers. Required constraints:

```text
UNIQUE(comment_scan_runs.scan_key)
UNIQUE(collection_tasks.scan_slice_key), NULL values allowed
CHECK scan_slice_no IS NULL OR scan_slice_no >= 0
CHECK target_pages IS NULL OR target_pages >= 0
CHECK next_page_number IS NULL OR next_page_number > 0
CHECK all scan counters >= 0
```

Use `SET NULL` foreign keys from tasks/coverage/raw pages/comment observations to scan runs. Keep `SnapshotCohortComponent.comment_scan_run_id` as the existing nullable logical link and add an index on it only if metadata does not already contain one.

- [x] **Step 4: Write the reversible Alembic revision**

The upgrade creates `comment_scan_runs`, adds the six referring columns, creates indexes and the all-status unique slice index, and updates PostgreSQL enum types with the complete scan mode/status values if native enums are used. The downgrade drops referring indexes/columns before dropping the run table. SQLite uses `batch_alter_table` where required.

- [x] **Step 5: Verify migration head and round trip**

```powershell
uv run pytest tests/test_schema_migrations.py::test_hot_comment_scan_revision_round_trip -q
uv run pytest tests/test_comment_scan_models.py tests/test_schema_migrations.py -q
```

Expected: upgrade to `0011_hot_comment_scans`, downgrade to `0010_snapshot_cohort_planning_job`, and re-upgrade all pass on a disposable SQLite database.

- [x] **Step 6: Commit the schema unit**

```powershell
git add alembic/versions/0011_hot_comment_scans.py books_of_time/db/models.py books_of_time/db/migrations.py books_of_time/domain/enums.py tests/test_schema_migrations.py tests/test_comment_scan_models.py
git commit -m "feat(scans): add persistent comment scan runs"
```

---

### Task 3: Scan Repositories And All-Status Slice Enqueue

**Files:**
- Create: `books_of_time/db/comment_scan_repositories.py`
- Modify: `books_of_time/db/repositories.py`
- Create: `tests/test_comment_scan_repositories.py`
- Create: `tests/test_hot_scan_postgresql.py`
- Modify: `tests/test_task_queue.py`

**Interfaces:**
- Produces frozen `HotScanRunPlan(scan_key, bvid, snapshot_cohort_id, mode, target_pages, start_page, end_page, policy_version, extra)`.
- Produces `CommentScanRunRepository.materialize_hot(plan, *, now) -> tuple[CommentScanRun, bool]`.
- Produces `CommentScanRunRepository.lock(scan_run_id) -> CommentScanRun`.
- Produces `mark_running`, `record_page_requested`, `record_page_succeeded`, `mark_paused`, `mark_complete`, and `mark_failed` methods that flush but never commit.
- Extends `CollectionTaskRepository.enqueue(..., comment_scan_run_id=None, scan_slice_no=None, scan_slice_key=None)`.

- [ ] **Step 1: Write failing repository tests**

Cover these cases with real SQLite sessions:

1. Repeated `materialize_hot()` returns the same row and preserves immutable identity.
2. A conflicting target/range for the same `scan_key` raises `ValueError`.
3. Page request increments before transport; page success advances `next_page_number` and cumulative counters once.
4. Pause/complete/failed transitions store orthogonal `status` and `outcome` values.
5. Enqueueing the same `scan_slice_key` returns the original task after it is succeeded; it never creates a second row.
6. Reusing a slice key with another scan run, slice number, or cohort component raises `ValueError`.
7. An injected unique-key race is recovered with a savepoint and reload, preserving the caller transaction.
8. When `BOT_TEST_POSTGRESQL_URL` is set, two independent PostgreSQL sessions racing on one slice key resolve to one row inside an isolated schema; otherwise this integration test skips with a clear reason.

- [ ] **Step 2: Run repository tests and verify RED**

```powershell
uv run pytest tests/test_comment_scan_repositories.py tests/test_task_queue.py -q
```

Expected: repository imports and enqueue parameters fail.

- [ ] **Step 3: Implement scan-run state methods**

`materialize_hot()` uses a savepoint around insert and reloads the unique winner. Identity validation compares BVID, cohort, mode, target pages, start page, end page, and policy version. Counter methods reject regressions, page numbers outside `[start_page, end_page]`, and updates after terminal status.

- [ ] **Step 4: Implement all-status slice enqueue**

When `scan_slice_key` is non-NULL, require `comment_scan_run_id` and `scan_slice_no`. Query it across every task status before insert. On `IntegrityError`, reload by `scan_slice_key` first, then fall back to active `idempotency_key` recovery only when no slice winner exists. Validate scan/cohort ownership before returning an existing task.

- [ ] **Step 5: Run focused repository and queue tests**

```powershell
uv run pytest tests/test_comment_scan_repositories.py tests/test_task_queue.py -q
uv run ruff check books_of_time/db/comment_scan_repositories.py books_of_time/db/repositories.py tests/test_comment_scan_repositories.py tests/test_task_queue.py
```

Expected: all tests pass and Ruff is clean.

If an isolated PostgreSQL URL is available, also run:

```powershell
$env:BOT_TEST_POSTGRESQL_URL = $env:BOT_DATABASE_URL
uv run pytest tests/test_hot_scan_postgresql.py -q
Remove-Item Env:BOT_TEST_POSTGRESQL_URL
```

- [ ] **Step 6: Commit the repository unit**

```powershell
git add books_of_time/db/comment_scan_repositories.py books_of_time/db/repositories.py tests/test_comment_scan_repositories.py tests/test_hot_scan_postgresql.py tests/test_task_queue.py
git commit -m "feat(tasks): add all-status scan slice identity"
```

---

### Task 4: Planner Hot Core And Deep Component Graph

**Files:**
- Modify: `books_of_time/task_orchestrator/snapshot_cohort_planner.py`
- Modify: `books_of_time/db/cohort_repositories.py`
- Modify: `tests/test_snapshot_cohort_planner.py`
- Modify: `tests/test_cohort_materialization.py`

**Interfaces:**
- Consumes `hot_page_plan()` and `CommentScanRunRepository.materialize_hot()`.
- Produces exact `hot_core`/`hot_deep` `CohortComponentPlan` page ranges and slice settings.
- Live materialization creates one `CommentScanRun`, links `SnapshotCohortComponent.comment_scan_run_id`, and enqueues slice zero atomically.
- Shadow materialization persists component page targets but creates no scan run and no task.

- [ ] **Step 1: Write failing tier matrix planner tests**

For each effective tier, assert active routine components:

```text
S hot_core planned_pages=3, pages 1..3
A hot_core planned_pages=2, pages 1..2
B hot_core planned_pages=1, page 1
C hot_core planned_pages=1, page 1
```

For checkpoint and first active adoption, assert totals and remainders:

```text
S hot_core 1..3 plus hot_deep 4..20 (17 pages)
A hot_core 1..2 plus hot_deep 3..10 (8 pages)
B hot_core page 1 plus hot_deep 2..3 (2 pages)
C hot_core page 1 and no hot_deep component
```

Assert dormant routine is one core page and archived has no hot component. Assert checkpoint components created from an existing cohort keep its frozen tier instead of a later reassessment.

- [ ] **Step 2: Run planner tests and verify RED**

```powershell
uv run pytest tests/test_snapshot_cohort_planner.py -q
```

Expected: every hot component still plans one page and no `hot_deep` exists.

- [ ] **Step 3: Implement component plan generation**

Replace the one-page special case in `_component_plan()` with hot-specific builders. Persist in each component `extra` and first task payload:

```json
{
  "scan_mode": "hot_core",
  "start_page": 1,
  "end_page": 3,
  "target_pages": 3,
  "max_pages_per_slice": 10,
  "max_scan_seconds": 55
}
```

Use `hot_deep` only when its remainder is positive. Add it to recovery ordering. When recovery combines overdue checkpoint components, preserve the missed component's frozen page range and choose the maximum required range for that recovery key rather than recalculating from the current tier.

- [ ] **Step 4: Write failing live materialization tests**

Materialize one live S checkpoint and assert:

```text
two hot scan runs, modes hot_core/hot_deep
component.comment_scan_run_id points to its run
slice zero keys are "<run_id>:hot_core:0" and "<run_id>:hot_deep:0"
hot_core task starts page 1 and ends page 3
hot_deep task starts page 4 and ends page 20
repeated materialization creates zero new runs/tasks
shadow materialization creates zero runs/tasks
```

- [ ] **Step 5: Implement atomic hot scan materialization**

After a hot component row exists and before its first task is inserted, materialize the run using scan key `{cohort_key}:{component_kind}`. Set `component.comment_scan_run_id`, enqueue slice zero with all three scan fields, and retain the existing component-level active idempotency key as a secondary guard. Caller commit/rollback remains authoritative.

- [ ] **Step 6: Run planner/materialization tests**

```powershell
uv run pytest tests/test_snapshot_cohort_planner.py tests/test_cohort_materialization.py tests/test_comment_scan_repositories.py -q
uv run ruff check books_of_time/task_orchestrator/snapshot_cohort_planner.py books_of_time/db/cohort_repositories.py tests/test_snapshot_cohort_planner.py tests/test_cohort_materialization.py
```

Expected: all tests pass and shadow still creates no executable work.

- [ ] **Step 7: Commit the planner unit**

```powershell
git add books_of_time/task_orchestrator/snapshot_cohort_planner.py books_of_time/db/cohort_repositories.py tests/test_snapshot_cohort_planner.py tests/test_cohort_materialization.py
git commit -m "feat(planner): plan hot core and deep scans"
```

---

### Task 5: Numbered Hot Collector Slices And Page Evidence

**Files:**
- Modify: `books_of_time/collectors/hot_comments.py`
- Modify: `books_of_time/db/repositories.py`
- Modify: `books_of_time/app.py`
- Modify: `tests/test_hot_comments_worker.py`
- Modify: `tests/test_comment_repositories.py`

**Interfaces:**
- Legacy hot tasks with `comment_scan_run_id=None` keep current `page`/`page_limit` behavior.
- Scan tasks use `CommentScanRun.next_page_number` as authoritative resume state.
- `RawPageObservationRepository.insert_from_parsed_page(..., scan_run_id=None)` and `CommentRepository.upsert_page(..., scan_run_id=None)` persist direct scan evidence.
- `HotCommentCollector` accepts injectable `monotonic` and `now` callables for deterministic slicing tests.

- [ ] **Step 1: Write failing multi-slice collector tests**

Use a fake 20-page client and deterministic clocks to assert:

1. Slice zero requests at most pages 4..13 for an S `hot_deep` run.
2. It marks the run paused, advances `next_page_number` to 14, and inserts exactly one slice-one task with key `<run_id>:hot_deep:1`.
3. Running slice one collects pages 14..20 and marks the run complete.
4. A clock crossing 55 seconds yields early even before ten pages and the next task resumes at the first uncollected page.
5. An explicit `cursor.is_end=true` or empty successful page marks `outcome=server_end` and creates no follow-up.
6. Repeated RPID appearances across pages retain append-only observations with direct `scan_run_id` evidence.
7. Existing non-scan `page_limit=2` behavior remains unchanged.

- [ ] **Step 2: Run hot collector tests and verify RED**

```powershell
uv run pytest tests/test_hot_comments_worker.py tests/test_comment_repositories.py -q
```

Expected: scan-run fields are ignored, all target pages run in one task, and evidence has no scan ID.

- [ ] **Step 3: Implement scan-aware page persistence**

Refactor `_collect_page()` to return `ParsedCommentPage` plus observation count. Pass `scan_run_id` into raw-page and comment observation writes. Treat `parsed.extra.get("is_end") is True` or `len(parsed.comments) == 0` as server end; do not infer end solely from `all_count`.

- [ ] **Step 4: Implement bounded numbered slices**

For scan tasks:

```text
lock scan run
mark running and resolve oid once
loop from next_page_number
before each request, stop if elapsed >= max_scan_seconds after at least one page
stop after max_pages_per_slice pages
record requested before HTTP and success after raw+parse+normalization
on target/server end, mark complete
otherwise mark paused and enqueue slice_no + 1 in the same session
```

The follow-up copies priority, budget, max retries, cohort IDs, run ID, and immutable range settings. Its ordinary idempotency key is `{scan.scan_key}:{mode}:active:{next_slice_no}` and its all-status key is `{scan.id}:{mode}:{next_slice_no}`.

- [ ] **Step 5: Preserve partial progress on request/parse failures**

Catch page-level exceptions only to update run counters and bounded `last_error_type`/`last_error_message`, then re-raise so the worker's existing retry/backoff path remains active. Already durable pages and `next_page_number` stay in the same outer transaction and are committed with failed coverage; a retry of the same task resumes from run state instead of replaying successful pages.

- [ ] **Step 6: Run hot and evidence tests**

```powershell
uv run pytest tests/test_hot_comments_worker.py tests/test_comment_repositories.py tests/test_http_request_attempts.py -q
uv run ruff check books_of_time/collectors/hot_comments.py books_of_time/db/repositories.py books_of_time/app.py tests/test_hot_comments_worker.py tests/test_comment_repositories.py
```

Expected: numbered slices, early server end, direct evidence IDs, and legacy tasks all pass.

- [ ] **Step 7: Commit the collector unit**

```powershell
git add books_of_time/collectors/hot_comments.py books_of_time/db/repositories.py books_of_time/app.py tests/test_hot_comments_worker.py tests/test_comment_repositories.py
git commit -m "feat(comments): collect hot scans in numbered slices"
```

---

### Task 6: Scan-Aware Coverage And Cohort Completion

**Files:**
- Modify: `books_of_time/db/repositories.py`
- Modify: `books_of_time/db/cohort_repositories.py`
- Modify: `books_of_time/worker.py`
- Modify: `tests/test_worker_cohort_lifecycle.py`
- Modify: `tests/test_coverage_repositories.py`

**Interfaces:**
- Coverage rows copy `task.comment_scan_run_id` directly.
- `SnapshotCohortExecutionRepository` synchronizes hot component counters from its scan run.
- A paused/running scan keeps the component and cohort active even when the current task coverage is partial.
- A complete/partial/failed/corrupted scan maps to the corresponding terminal component state.

- [ ] **Step 1: Write failing two-slice lifecycle tests**

Run a live S hot-deep component through two worker tasks and assert after slice zero:

```text
current task succeeded
coverage status partial, reason time_slice_yield
next slice pending
scan status paused
component status running, finished_at NULL
component requested/succeeded counters equal scan counters
cohort remains running
```

After slice one, assert scan/component complete, component counters equal 17 successful deep pages, parent cohort aggregates normally, and both coverage rows carry the same `comment_scan_run_id`.

Add terminal retry exhaustion and parse-corruption cases. A terminal task failure must mark the active scan failed before component aggregation; a raw-saved parse failure must retain raw/scan evidence and must not become complete.

- [ ] **Step 2: Run lifecycle tests and verify RED**

```powershell
uv run pytest tests/test_worker_cohort_lifecycle.py tests/test_coverage_repositories.py -q
```

Expected: the first partial slice prematurely marks the component/cohort partial and coverage lacks scan ID.

- [ ] **Step 3: Propagate scan identity into coverage**

Set `comment_scan_run_id=task.comment_scan_run_id` in success and failed coverage insertion paths. Existing rows/tasks remain nullable.

- [ ] **Step 4: Implement scan-authoritative component aggregation**

When a linked task has a scan run:

```text
copy pages_requested/pages_succeeded/items_observed/raw_payloads_saved from the run
running or paused scan -> component running, unfinished
complete scan -> component complete
partial scan -> component partial
failed scan -> component failed
corrupted scan -> component corrupted
```

Do not sum the same cumulative scan counters per slice. Non-scan components retain the existing incremental coverage path. On a terminal worker exception, mark a non-terminal linked scan failed with `outcome=retry_exhausted` before recomputing the cohort.

- [ ] **Step 5: Run worker, coverage, and hot scan tests**

```powershell
uv run pytest tests/test_worker_cohort_lifecycle.py tests/test_coverage_repositories.py tests/test_hot_comments_worker.py -q
uv run ruff check books_of_time/db/repositories.py books_of_time/db/cohort_repositories.py books_of_time/worker.py tests/test_worker_cohort_lifecycle.py tests/test_coverage_repositories.py
```

Expected: all tests pass and multi-slice components remain active until the logical scan closes.

- [ ] **Step 6: Commit the lifecycle unit**

```powershell
git add books_of_time/db/repositories.py books_of_time/db/cohort_repositories.py books_of_time/worker.py tests/test_worker_cohort_lifecycle.py tests/test_coverage_repositories.py
git commit -m "feat(cohorts): track hot scan completion across slices"
```

---

### Task 7: C4 Documentation, PostgreSQL Concurrency, And Acceptance

**Files:**
- Modify: `docs/CONFIGURATION.md`
- Modify: `docs/COLLECTION.md`
- Modify: `docs/DATA_MODEL.md`
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/TODO.md`
- Modify: `docs/superpowers/plans/2026-07-14-hot-core-deep-scans-c4.md`

**Interfaces:**
- Documents page targets, first-adoption/checkpoint behavior, numbered slices, scan statuses/outcomes, server-end semantics, evidence links, and C4/C7 ownership boundary.
- Marks only C4 complete; C5-C9 remain unchecked.

- [ ] **Step 1: Run the opt-in PostgreSQL race test**

When `BOT_TEST_POSTGRESQL_URL` is set, the Task 3 integration test creates an isolated schema, runs revision head, and uses two independent sessions to enqueue the same `scan_slice_key`. Both callers must resolve to one task row and the surrounding transaction must remain usable. A skip is acceptable only when the environment variable is absent; the standard SQLite suite remains mandatory.

- [ ] **Step 2: Run migration and focused C4 verification**

```powershell
uv run pytest tests/test_schema_migrations.py::test_hot_comment_scan_revision_round_trip -q
uv run pytest tests/test_hot_scan_policy.py tests/test_comment_scan_models.py tests/test_comment_scan_repositories.py tests/test_snapshot_cohort_planner.py tests/test_cohort_materialization.py tests/test_hot_comments_worker.py tests/test_worker_cohort_lifecycle.py -q
```

If an isolated PostgreSQL URL is available:

```powershell
$env:BOT_TEST_POSTGRESQL_URL = $env:BOT_DATABASE_URL
uv run pytest tests/test_hot_scan_postgresql.py -q
Remove-Item Env:BOT_TEST_POSTGRESQL_URL
```

- [ ] **Step 3: Document the complete operator and data flow**

Document:

```text
cohort-default-v2 requirement
routine 3/2/1/1 and checkpoint/initial 20/10/3/1 targets
hot_core versus hot_deep page ranges
10-page and 55-second slice limits
all-status slice keys and restart resume behavior
scan run status versus outcome
server end and partial/failure meanings
raw/page/comment/coverage scan evidence links
shadow creates plans only and C7 still owns live activation
PostgreSQL multi-worker expectations and SQLite single-process limit
```

- [ ] **Step 4: Perform a P0/P1 audit**

Review duplicate next-slice prevention, task retry after partial page success, scan counter monotonicity, server-end off-by-one behavior, first-adoption deep planning, checkpoint total versus remainder, recovery frozen ranges, shadow no-run/no-task enforcement, terminal worker failure, and scan evidence propagation. Every confirmed P0/P1 bug receives a failing regression test, a separate code commit, and a numbered `docs/fix/2026-07-14_<no>.md` record.

- [ ] **Step 5: Run complete verification**

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
git diff --check
```

Expected: full suite passes, Ruff is clean, every file is formatted, and no whitespace error exists.

- [ ] **Step 6: Mark C4 complete and C5 next**

Change C4 from `[ ]` to `[x]`, leave C5-C9 unchecked, and update Near-term Sprint to identify C5 Latest Scan Runs And Automatic Baseline as the next stage. Do not mark the overall Collection-First Snapshot Cohorts mainline complete.

- [ ] **Step 7: Commit documentation and completion state**

```powershell
git add docs/CONFIGURATION.md docs/COLLECTION.md docs/DATA_MODEL.md docs/OPERATIONS.md docs/TODO.md docs/superpowers/plans/2026-07-14-hot-core-deep-scans-c4.md
git commit -m "docs: complete hot core and deep scans C4"
```

## Plan Self-Review Result

- **Spec coverage:** Tasks 1-6 cover versioned page targets, persistent scan identity, all-status slice uniqueness, tiered planner components, initial/checkpoint deep ranges, 10-page/55-second continuations, server end, evidence links, and scan-aware cohort completion. Task 7 covers migration, PostgreSQL concurrency, documentation, audit, and acceptance. C5 retains latest CAS/frontiers and the single-active-latest constraint; C7 retains live owner transfer and capacity gates.
- **Placeholder scan:** No TBD, unnamed error handling, generic testing instruction, or unresolved implementation choice remains. Every task has exact interfaces, RED/GREEN commands, and a commit boundary.
- **Type consistency:** `HotCommentPolicy`, `HotPagePlan`, `CommentScanRun`, `HotScanRunPlan`, task scan fields, repository methods, component links, and evidence fields use the same names throughout the plan.
- **Execution choice:** The user authorized autonomous inline execution on `main` and requested conservative subagent use. Execute this plan in the main thread with TDD and do not pause for design approval.
