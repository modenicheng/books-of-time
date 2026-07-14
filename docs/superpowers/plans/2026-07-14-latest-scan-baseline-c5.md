# Latest Scan Runs And Automatic Baseline C5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make latest-comment collection a persistent, single-owner, restart-safe logical scan that automatically progresses baseline tail -> linked head sweep -> multi-anchor frontier and then runs incremental continuations for snapshot cohorts.

**Architecture:** Extend `frontier_states` with an active scan owner, monotonic compare-and-swap version, and ordered frontier anchors. Keep the existing legacy latest collector unchanged for manual tasks without a scan ID, while scan-backed cohort tasks use a new focused collector and the existing all-status numbered slice identity. A live cohort component either creates one latest scan or joins the BVID's current scan; scan completion fans current-head evidence out to every linked cohort component. Service rollout remains shadow-only until C7.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async ORM, Alembic, PostgreSQL partial indexes/row locking, SQLite deterministic tests, pytest, Ruff, existing Bilibili latest parser/raw/comment/media repositories.

## Global Constraints

- C5 must not enable `snapshot_cohorts.rollout_mode: live`; C7 retains scheduler ownership migration, legacy-task draining/mapping, capacity gates, page-level short transactions, and lease renewal.
- Legacy `FETCH_LATEST_COMMENTS` tasks with `comment_scan_run_id=NULL` retain the current frontier-state behavior and CLI compatibility.
- C5 implements only `baseline_tail`, `baseline_head_sweep`, and `incremental`. `full_reconciliation` and `segmented_reconciliation` remain C6 behavior even though their enum values and active-uniqueness coverage already exist.
- One BVID has at most one active latest-mode scan across `planned`, `running`, and `paused`.
- `frontier_states` is the authoritative physical cursor/retry owner for scan-backed latest collection. `comment_scan_runs` is the immutable logical history after terminal status.
- Every frontier mutation uses monotonic compare-and-swap. Opaque cursor strings are compared only for equality and are never ordered.
- Frontier anchors contain at most five ordered `{rpid, platform_created_at}` records. Matching any retained RPID closes a head or incremental scan.
- An empty comment section establishes an explicit empty frontier; it is not a missing-anchor corruption.
- A baseline tail page cannot satisfy a current-head cohort component. Only a successful head-sweep or incremental head page captured in `[scheduled_for, deadline)` can satisfy it.
- A numbered latest slice runs for `min(55, max(10, floor(effective_interval_seconds * 0.4)))` seconds when an interval is available, otherwise 55 seconds.
- Cursor retry state persists across numbered slices. The same cursor receives at most the configured attempts and backoffs.
- Raw pages, comment observations, coverage, tasks, components, and scan runs retain direct scan evidence where the schema supports it. Public user identifiers remain visible and media remains local filesystem storage.
- PostgreSQL is the multi-worker target. SQLite remains single-process development/test behavior and is not accepted as proof of row-lock concurrency.
- Never downgrade the configured PostgreSQL database. Migration downgrade tests use disposable SQLite databases or an isolated disposable PostgreSQL schema.
- Use TDD for every behavior change and one Conventional Commit per task. A confirmed P0/P1 bug found during audit receives its own regression-test commit plus numbered `docs/fix/2026-07-14_<no>.md` commit.

---

### Task 1: Ordered Frontier Anchors And Slice Timing

**Files:**
- Create: `books_of_time/domain/latest_frontier.py`
- Create: `tests/test_latest_frontier_policy.py`

**Interfaces:**
- Produces `MAX_FRONTIER_ANCHORS = 5`.
- Produces `anchors_from_comments(comments) -> tuple[dict[str, object], ...]`.
- Produces `normalize_anchor_set(value) -> tuple[dict[str, object], ...]`.
- Produces `anchor_rpids(anchors) -> frozenset[int]` and `primary_anchor(anchors) -> tuple[int | None, datetime | None]`.
- Produces `page_matches_anchor(comments, anchors) -> bool`.
- Produces `latest_slice_seconds(effective_interval_seconds: float | int | None) -> int`.

- [x] **Step 1: Write failing anchor and timing tests**

Assert:

```python
comments = [
    parsed_comment(rpid=101, platform_created_at=t0),
    parsed_comment(rpid=102, platform_created_at=None),
    parsed_comment(rpid=103, platform_created_at=t1),
    parsed_comment(rpid=104, platform_created_at=t2),
    parsed_comment(rpid=105, platform_created_at=t3),
    parsed_comment(rpid=106, platform_created_at=t4),
]

anchors = anchors_from_comments(comments)
assert [item["rpid"] for item in anchors] == [101, 102, 103, 104, 105]
assert anchors[0]["platform_created_at"] == t0.isoformat()
assert anchors[1]["platform_created_at"] is None
assert anchor_rpids(anchors) == frozenset({101, 102, 103, 104, 105})
assert page_matches_anchor([parsed_comment(rpid=105)], anchors) is True
assert primary_anchor(anchors) == (101, t0)
assert anchors_from_comments([]) == ()

assert latest_slice_seconds(None) == 55
assert latest_slice_seconds(60) == 24
assert latest_slice_seconds(120) == 48
assert latest_slice_seconds(600) == 55
assert latest_slice_seconds(1) == 10
```

Also reject Boolean/non-positive intervals, malformed anchor JSON, more than five anchors, duplicate/non-positive RPIDs, and naive/invalid timestamp strings.

- [x] **Step 2: Run tests and verify RED**

```powershell
uv run pytest tests/test_latest_frontier_policy.py -q
```

Expected: import failure for `books_of_time.domain.latest_frontier`.

- [x] **Step 3: Implement the pure helpers**

Store timestamps as UTC ISO-8601 strings, preserve order, return immutable tuples, and do not infer ordering from RPIDs or cursor values. `latest_slice_seconds()` uses `math.floor`, clamps to `[10, 55]`, and treats `None` as the 55-second default.

- [x] **Step 4: Run focused tests and Ruff**

```powershell
uv run pytest tests/test_latest_frontier_policy.py -q
uv run ruff check books_of_time/domain/latest_frontier.py tests/test_latest_frontier_policy.py
```

Expected: all tests pass and Ruff is clean.

- [x] **Step 5: Commit the pure policy unit**

```powershell
git add books_of_time/domain/latest_frontier.py tests/test_latest_frontier_policy.py docs/superpowers/plans/2026-07-14-latest-scan-baseline-c5.md
git commit -m "feat(latest): add multi-anchor frontier policy"
```

---

### Task 2: Frontier Ownership Schema And Active Latest Uniqueness

**Files:**
- Create: `alembic/versions/0012_latest_comment_scans.py`
- Modify: `books_of_time/db/models.py`
- Modify: `books_of_time/db/schema.py`
- Modify: `tests/test_comment_scan_models.py`
- Modify: `tests/test_schema_migrations.py`

**Interfaces:**
- Extends `FrontierState` with `active_scan_run_id`, `version`, and `frontier_anchor_set`.
- Adds `idx_frontier_states_active_scan`.
- Adds partial unique index `uq_comment_scan_runs_active_latest_bvid` for the latest modes `baseline_tail`, `baseline_head_sweep`, `incremental`, `full_reconciliation`, and `segmented_reconciliation` while status is `planned`, `running`, or `paused`.
- Produces Alembic head `0012_latest_comment_scans`.

- [x] **Step 1: Write failing model contract tests**

Use real SQLite sessions to assert:

```python
state.active_scan_run_id = scan.id
state.version = 3
state.frontier_anchor_set = [{"rpid": 1001, "platform_created_at": None}]
```

persists and reloads. Assert a second active latest scan for the same BVID fails even with another latest mode, while a terminal latest scan and an active `hot_core` scan may coexist. Assert `version < 0` fails and deleting a scan sets `active_scan_run_id` to NULL.

- [x] **Step 2: Run model tests and verify RED**

```powershell
uv run pytest tests/test_comment_scan_models.py -q
```

Expected: missing frontier columns/index behavior.

- [x] **Step 3: Implement ORM columns, checks, and indexes**

Use the existing JSON and UTC helpers. Required model contract:

```text
active_scan_run_id BIGINT NULL REFERENCES comment_scan_runs(id) ON DELETE SET NULL
version INTEGER NOT NULL DEFAULT 0 CHECK version >= 0
frontier_anchor_set JSON NOT NULL DEFAULT []
```

The partial unique index must use equivalent PostgreSQL and SQLite predicates and must not include hot/reply/visibility modes.

- [x] **Step 4: Write reversible Alembic revision**

Upgrade adds the columns/FK/check/index and active-latest partial unique index, then backfills each existing non-NULL `frontier_rpid` as a one-element anchor set with unknown platform time. Downgrade drops indexes/constraints before columns. Use `batch_alter_table` for SQLite and preserve revision round-trip support.

- [x] **Step 5: Run migration and model verification**

```powershell
uv run pytest tests/test_schema_migrations.py::test_latest_comment_scan_revision_round_trip -q
uv run pytest tests/test_comment_scan_models.py tests/test_schema_migrations.py -q
```

Expected: upgrade to `0012_latest_comment_scans`, downgrade to `0011_hot_comment_scans`, and re-upgrade pass on a disposable SQLite database.

- [x] **Step 6: Commit the schema unit**

```powershell
git add alembic/versions/0012_latest_comment_scans.py books_of_time/db/models.py books_of_time/db/schema.py tests/test_comment_scan_models.py tests/test_schema_migrations.py
git commit -m "feat(scans): add latest frontier ownership"
```

---

### Task 3: Frontier CAS And Latest Scan Repositories

**Files:**
- Create: `books_of_time/db/latest_scan_repositories.py`
- Modify: `books_of_time/db/repositories.py`
- Create: `tests/test_latest_scan_repositories.py`
- Create: `tests/test_latest_scan_postgresql.py`

**Interfaces:**
- Produces `FrontierVersionConflict(RuntimeError)`.
- Produces frozen `FrontierStateUpdate` carrying the complete mutable frontier snapshot.
- Extends `FrontierStateRepository.get_or_create(..., lock=False)` with race-safe insert/reload.
- Produces `FrontierStateRepository.compare_and_swap(state_id, expected_version, update, *, now) -> FrontierState`.
- Produces frozen `LatestScanRunPlan(scan_key, bvid, snapshot_cohort_id, parent_scan_run_id, mode, policy_version, reason, start_frontier_rpid, start_anchor_set, start_cursor, extra)`.
- Produces `LatestScanClaim(scan, frontier_state, created)`.
- Produces `LatestScanRunRepository.claim_or_join(plan, *, frontier_state, expected_version, now) -> LatestScanClaim`.
- Produces latest scan counter/status methods that flush but never commit.

- [x] **Step 1: Write failing CAS and claim tests**

Cover:

1. `get_or_create(lock=True)` is idempotent and a unique insert race reloads the winner without rolling back the caller transaction.
2. CAS with the current version updates cursor/anchors/active owner and increments version exactly once.
3. Reusing a stale expected version raises `FrontierVersionConflict` and changes no fields.
4. `claim_or_join()` creates one latest scan, sets `active_scan_run_id`, and returns the post-CAS version.
5. A second plan for the same BVID joins the active scan instead of creating another row.
6. A dangling/terminal active pointer is cleared before a new claim; an active pointer to another BVID is rejected.
7. Latest scan request/success counters are monotonic; success requires a recorded request and updates `result_cursor`, anchor evidence, item/raw counts once.
8. Terminal scans cannot be resumed or mutated.

- [x] **Step 2: Run repository tests and verify RED**

```powershell
uv run pytest tests/test_latest_scan_repositories.py -q
```

Expected: repository module/interfaces do not exist.

- [x] **Step 3: Implement compare-and-swap**

Use one SQL `UPDATE frontier_states SET ..., version=version+1 WHERE id=:id AND version=:expected`. Require `rowcount == 1`, then refresh the ORM row. The update replaces `extra` and `frontier_anchor_set` with validated copies so in-place JSON mutation cannot bypass versioning.

- [x] **Step 4: Implement active scan claim/join**

Allowed modes are the five latest modes from Task 2; C5 callers create only baseline tail/head/incremental. Lock the frontier row, prefer its valid active owner, otherwise query the partial-index domain, then insert under a savepoint. On unique conflict reload the active winner. Validate BVID, mode domain, immutable scan key identity, parent relation, policy version, and normalized anchors.

- [x] **Step 5: Add opt-in PostgreSQL concurrency test**

When `BOT_TEST_POSTGRESQL_URL` is set, create an isolated disposable schema at Alembic head. Use two independent sessions to race different `scan_key` values for the same BVID. Assert both callers resolve to one active latest scan, one frontier owner, monotonic versioning, and usable surrounding transactions. Skip clearly when the environment variable is absent.

- [x] **Step 6: Run repository verification**

```powershell
uv run pytest tests/test_latest_scan_repositories.py -q
uv run ruff check books_of_time/db/latest_scan_repositories.py books_of_time/db/repositories.py tests/test_latest_scan_repositories.py tests/test_latest_scan_postgresql.py
```

If local PostgreSQL is configured, also run the isolated test with `BOT_TEST_POSTGRESQL_URL` set from the configured database URL.

- [x] **Step 7: Commit the repository unit**

```powershell
git add books_of_time/db/latest_scan_repositories.py books_of_time/db/repositories.py tests/test_latest_scan_repositories.py tests/test_latest_scan_postgresql.py
git commit -m "feat(scans): coordinate active latest scans"
```

---

### Task 4: Cohort Latest Materialization And Join Semantics

**Files:**
- Modify: `books_of_time/task_orchestrator/snapshot_cohort_planner.py`
- Modify: `books_of_time/db/cohort_repositories.py`
- Modify: `tests/test_snapshot_cohort_planner.py`
- Modify: `tests/test_cohort_materialization.py`

**Interfaces:**
- Consumes `latest_slice_seconds()` and `LatestScanRunRepository.claim_or_join()`.
- Adds latest component payload fields `max_scan_seconds` and `current_head_required=true`.
- Live materialization creates or joins one latest scan and sets `SnapshotCohortComponent.comment_scan_run_id`.
- Newly owned scans enqueue slice zero with all-status slice identity and `frontier_version` in payload.
- Joined components use `joined_active_task` and create no duplicate task.

- [x] **Step 1: Write failing planner timing tests**

Assert routine latest payload limits:

```text
effective interval 60 seconds  -> max_scan_seconds 24
effective interval 120 seconds -> max_scan_seconds 48
effective interval >= 138 sec  -> max_scan_seconds 55
checkpoint/recovery            -> max_scan_seconds 55
```

Existing shadow plans must keep `latest_current_head` / `latest_reconciliation` but create no scan/task.

- [x] **Step 2: Write failing live materialization tests**

Cover:

1. No baseline -> one `baseline_tail` run, frontier owner, linked component, and slice zero.
2. Legacy `baseline_tail_complete` state -> one `baseline_head_sweep` run seeded from the saved baseline anchor.
3. Complete frontier -> one `incremental` run seeded with up to five anchors.
4. A second cohort while a scan is active links it as `joined_active_task`, creates no task, and does not change the first scan's immutable identity.
5. Repeated materialization returns the same scan/task.
6. Shadow creates component plans only: zero frontier owner, scan runs, or tasks.

- [x] **Step 3: Run planner/materializer tests and verify RED**

```powershell
uv run pytest tests/test_snapshot_cohort_planner.py tests/test_cohort_materialization.py -q
```

Expected: latest components still enqueue generic tasks and have no scan ownership.

- [x] **Step 4: Implement latest component payload timing**

Pass the already computed routine interval into component-plan creation. Checkpoint and recovery use 55. Do not alter hot page ranges or C3/C4 shadow behavior.

- [x] **Step 5: Implement atomic live latest materialization**

Before generic task insertion, lock/create the `latest_comments` frontier. Select the initial mode from durable state:

```text
baseline_status == baseline_complete      -> incremental
baseline_status == baseline_tail_complete -> baseline_head_sweep
otherwise                                 -> baseline_tail
```

For a newly claimed scan, link the component and enqueue slice zero with key `<scan_id>:<mode>:0`; payload includes `scan_mode`, `frontier_version`, `max_scan_seconds`, and `current_head_required`. For an existing active scan, link the component, set `joined_active_task`, and do not create a task. `latest_reconciliation` uses the same current-head incremental mode in C5; it must not claim full reconciliation coverage before C6.

- [x] **Step 6: Run focused verification**

```powershell
uv run pytest tests/test_snapshot_cohort_planner.py tests/test_cohort_materialization.py tests/test_latest_scan_repositories.py -q
uv run ruff check books_of_time/task_orchestrator/snapshot_cohort_planner.py books_of_time/db/cohort_repositories.py tests/test_snapshot_cohort_planner.py tests/test_cohort_materialization.py
```

- [x] **Step 7: Commit the planner/materializer unit**

```powershell
git add books_of_time/task_orchestrator/snapshot_cohort_planner.py books_of_time/db/cohort_repositories.py tests/test_snapshot_cohort_planner.py tests/test_cohort_materialization.py
git commit -m "feat(planner): attach cohorts to latest scans"
```

---

### Task 5: Scan-Backed Baseline Tail Slices And Evidence

**Files:**
- Create: `books_of_time/collectors/latest_scan.py`
- Modify: `books_of_time/collectors/latest_comments.py`
- Modify: `books_of_time/app.py`
- Create: `tests/test_latest_scan_worker.py`
- Modify: `tests/test_latest_comments_worker.py`

**Interfaces:**
- `LatestCommentCollector.collect()` dispatches scan-backed tasks to `LatestScanCollector` and preserves the existing legacy path for NULL scan IDs.
- `LatestScanCollector` accepts injectable `monotonic`, `sleep`, and `now` callables.
- Raw page and comment observation persistence passes `scan_run_id`.
- Follow-ups carry incremented slice number, all-status slice key, and post-CAS `frontier_version`.

- [x] **Step 1: Write failing dispatch and legacy-regression tests**

Assert a legacy task still produces NULL scan evidence and current baseline behavior. Assert a scan task missing slice identity or `frontier_version` fails before network access and terminal worker handling can close its scan.

- [x] **Step 2: Write failing baseline-tail slice tests**

With deterministic fake cursor pages, assert:

1. The first successful page records up to five ordered `start_anchor_set` entries and the primary `start_frontier_rpid`.
2. Each HTTP attempt increments `pages_requested`; each durable parsed page increments `pages_succeeded`, item/raw counts, and persists direct scan evidence.
3. A time budget yields `paused/time_slice_yield`, saves the next cursor through CAS, and inserts exactly one next numbered slice.
4. A process/task retry resumes from `frontier_states.cursor`; successful prior cursor pages are not replayed.
5. Retry attempts for one cursor persist across slices and corruption occurs only after the configured total.
6. Cursor repetition marks the scan corrupted and creates no follow-up.
7. A stale `frontier_version` raises `FrontierVersionConflict` and cannot persist page progress or a follow-up.

- [x] **Step 3: Run new tests and verify RED**

```powershell
uv run pytest tests/test_latest_scan_worker.py tests/test_latest_comments_worker.py -q
```

Expected: scan-backed tasks are handled by the legacy mutable state machine and lack scan evidence/slice identity.

- [x] **Step 4: Implement collector dispatch without changing legacy behavior**

Move no legacy semantics. Rename the existing body to `_collect_legacy()` and delegate only when `task.comment_scan_run_id` is non-NULL. Keep CLI/manual task output and existing tests unchanged.

- [x] **Step 5: Implement baseline-tail scan loop**

The scan loop reads only the cursor and retry fields owned by its active frontier row. For every successful page, archive raw before parse, persist raw page/comments/media with `scan_run_id`, update scan counters and frontier cursor/version, and assign the new version back to `task.payload`. Yield and follow-up insertion share the worker transaction.

At server end in this task, mark the parent tail `complete/tail_reached`; Task 6 replaces the temporary tail-complete endpoint with the automatic linked head transition. Empty first pages retain an empty anchor set.

- [x] **Step 6: Run collector verification**

```powershell
uv run pytest tests/test_latest_scan_worker.py tests/test_latest_comments_worker.py tests/test_comment_repositories.py -q
uv run ruff check books_of_time/collectors/latest_scan.py books_of_time/collectors/latest_comments.py books_of_time/app.py tests/test_latest_scan_worker.py tests/test_latest_comments_worker.py
```

- [x] **Step 7: Commit the baseline-tail unit**

```powershell
git add books_of_time/collectors/latest_scan.py books_of_time/collectors/latest_comments.py books_of_time/app.py tests/test_latest_scan_worker.py tests/test_latest_comments_worker.py
git commit -m "feat(comments): persist latest scans in numbered slices"
```

---

### Task 6: Atomic Tail-To-Head Baseline And Anchor Resilience

**Files:**
- Modify: `books_of_time/db/latest_scan_repositories.py`
- Modify: `books_of_time/collectors/latest_scan.py`
- Modify: `books_of_time/task_orchestrator/snapshot_cohort_planner.py`
- Modify: `books_of_time/db/cohort_repositories.py`
- Modify: `tests/test_latest_scan_repositories.py`
- Modify: `tests/test_latest_scan_worker.py`
- Modify: `tests/test_snapshot_cohort_planner.py`

**Interfaces:**
- Produces `LatestScanRunRepository.complete_tail_and_create_head(...) -> LatestScanClaim | None`.
- Produces idempotent planner repair for a terminal tail missing its derived child run/task.
- Rebinds all non-terminal components linked to the tail onto the child head scan.

- [x] **Step 1: Write failing atomic handoff tests**

Assert the transaction that reaches tail end:

```text
parent status/outcome = complete/tail_reached
child mode = baseline_head_sweep
child.parent_scan_run_id = parent.id
child.start_anchor_set = parent.start_anchor_set
frontier.active_scan_run_id = child.id
frontier.cursor = ""
one child slice zero exists
active cohort components now reference child and are joined_active_task
```

Repeated handoff/repair calls must resolve to the same child and task. Empty baseline tail creates an explicit complete empty frontier, clears the active owner, and does not issue a redundant head request.

- [x] **Step 2: Write failing head-sweep tests**

Cover:

1. The first head page records `result_anchor_set` and `head_captured_at`.
2. Seeing the second through fifth retained anchor completes even when the primary anchor disappeared.
3. Time slicing resumes from the saved cursor and preserves first-page result anchors.
4. Reaching server end with none of the non-empty start anchors marks `corrupted/start_anchor_missing`.
5. Completion writes `frontier_anchor_set`, compatibility `frontier_rpid/frontier_time`, `baseline_status=baseline_complete`, clears active owner/cursor, and creates no follow-up.

- [x] **Step 3: Run focused tests and verify RED**

```powershell
uv run pytest tests/test_latest_scan_repositories.py tests/test_latest_scan_worker.py tests/test_snapshot_cohort_planner.py -q
```

- [x] **Step 4: Implement atomic tail transition and empty frontier**

Derive child key as `<parent.scan_key>:baseline_head_sweep`. Create the child and slice under savepoints, CAS the frontier owner/cursor, and rebind only pending/running/joined components. The current parent task retains its own scan ID for coverage evidence.

- [x] **Step 5: Implement head sweep and planner repair**

Head matching uses RPID membership only; platform time remains evidence, not an equality requirement. The 30-second planner repair finds `complete/tail_reached` parents whose non-empty anchors lack the deterministic child/task and recreates the same identities. Shadow mode records no repairs that create executable work.

- [x] **Step 6: Run focused verification**

```powershell
uv run pytest tests/test_latest_scan_repositories.py tests/test_latest_scan_worker.py tests/test_snapshot_cohort_planner.py tests/test_cohort_materialization.py -q
uv run ruff check books_of_time/db/latest_scan_repositories.py books_of_time/collectors/latest_scan.py books_of_time/task_orchestrator/snapshot_cohort_planner.py books_of_time/db/cohort_repositories.py tests/test_latest_scan_repositories.py tests/test_latest_scan_worker.py
```

- [x] **Step 7: Commit the automatic baseline unit**

```powershell
git add books_of_time/db/latest_scan_repositories.py books_of_time/collectors/latest_scan.py books_of_time/task_orchestrator/snapshot_cohort_planner.py books_of_time/db/cohort_repositories.py tests/test_latest_scan_repositories.py tests/test_latest_scan_worker.py tests/test_snapshot_cohort_planner.py
git commit -m "feat(latest): automate baseline head sweep"
```

---

### Task 7: Incremental Continuation And Empty-Frontier Semantics

**Files:**
- Modify: `books_of_time/collectors/latest_scan.py`
- Modify: `books_of_time/db/latest_scan_repositories.py`
- Modify: `tests/test_latest_scan_worker.py`
- Modify: `tests/test_comment_repositories.py`

**Interfaces:**
- Implements scan-backed `incremental` mode using the persisted multi-anchor frontier.
- Produces `complete/frontier_reached`, `partial/frontier_missing`, `paused/time_slice_yield`, and `corrupted` outcomes without placing outcome text in status.

- [ ] **Step 1: Write failing incremental tests**

Cover:

1. Seeing any old anchor completes and replaces the frontier with the first current head page's ordered anchors.
2. Pause/follow-up preserves current head result anchors and resumes from the saved cursor.
3. An explicit prior empty frontier scans until server end, captures all newly reachable pages, and completes `frontier_reached` rather than `frontier_missing`.
4. A still-empty section completes with an empty frontier.
5. Reaching server end with non-empty old anchors absent marks `partial/frontier_missing`, records all missing anchor RPIDs in scan/state evidence, updates the current frontier candidate, and preserves the existing `missing_after_seen` visibility event behavior for the compatibility primary anchor.
6. Cursor loop/retry exhaustion is corrupted and never advances the official frontier.

- [ ] **Step 2: Run incremental tests and verify RED**

```powershell
uv run pytest tests/test_latest_scan_worker.py -q
```

Expected: unsupported scan mode or missing frontier update behavior.

- [ ] **Step 3: Implement incremental mode**

Use `scan.start_anchor_set` as immutable old frontier evidence and `scan.result_anchor_set` as the first successful current head candidate. Only a terminal reached/missing decision changes official `frontier_anchor_set`; paused/corrupted scans leave the official frontier unchanged. Continue to use `frontier_states.cursor` and CAS for physical resume.

- [ ] **Step 4: Run collector and visibility regression tests**

```powershell
uv run pytest tests/test_latest_scan_worker.py tests/test_latest_comments_worker.py tests/test_comment_repositories.py -q
uv run ruff check books_of_time/collectors/latest_scan.py books_of_time/db/latest_scan_repositories.py tests/test_latest_scan_worker.py
```

- [ ] **Step 5: Commit the incremental unit**

```powershell
git add books_of_time/collectors/latest_scan.py books_of_time/db/latest_scan_repositories.py tests/test_latest_scan_worker.py tests/test_comment_repositories.py
git commit -m "feat(latest): continue multi-anchor incremental scans"
```

---

### Task 8: Cohort Fan-Out, Deadlines, And Terminal Failure

**Files:**
- Modify: `books_of_time/db/cohort_repositories.py`
- Modify: `books_of_time/task_orchestrator/snapshot_cohort_planner.py`
- Modify: `books_of_time/worker.py`
- Modify: `tests/test_worker_cohort_lifecycle.py`
- Modify: `tests/test_snapshot_cohort_planner.py`
- Modify: `tests/test_latest_scan_worker.py`

**Interfaces:**
- Produces `SnapshotCohortExecutionRepository.sync_latest_scan_consumers(scan_run_id, *, finished_at)`.
- Produces planner repair/finalization for linked components whose active scan changed, closed, or crossed their deadline.
- Clears `frontier_states.active_scan_run_id` through CAS when a worker terminally fails an active latest task.

- [ ] **Step 1: Write failing component fan-out tests**

Materialize several cohorts linked to one scan and assert:

1. A tail scan never completes a current-head component.
2. Tail->head rebind keeps all unexpired consumers joined.
3. A head/incremental `head_captured_at` inside one component's `[scheduled_for, deadline)` completes that component even if the logical scan later pauses for deeper pages.
4. A component whose window begins after that head capture remains joined/pending and is rebound to the next scan rather than falsely completed.
5. At deadline, an unresolved tail consumer becomes partial `baseline_tail_in_progress`; another latest consumer becomes partial `current_head_not_captured`.
6. All affected parent cohorts are recomputed, not only the task-owning cohort.

- [ ] **Step 2: Write failing terminal failure tests**

Assert retryable task failure keeps scan/frontier ownership and components active. On retry exhaustion:

```text
transport/collector failure -> scan failed/retry_exhausted
parse failure               -> scan corrupted/retry_exhausted
frontier active owner       -> NULL through CAS
all linked components       -> failed/corrupted
all parent cohorts          -> recomputed terminal state
```

A stale worker version conflict must not clear a newer active owner.

- [ ] **Step 3: Run lifecycle tests and verify RED**

```powershell
uv run pytest tests/test_worker_cohort_lifecycle.py tests/test_snapshot_cohort_planner.py tests/test_latest_scan_worker.py -q
```

- [ ] **Step 4: Implement scan-consumer synchronization**

For latest modes, do not use the C4 one-component exact-link shortcut. Load the task scan, allow the task's completed tail to point at its deterministic child, then synchronize every component linked to the effective scan. Copy cumulative counters, evaluate head capture windows, and recompute each distinct cohort under row locks.

- [ ] **Step 5: Implement planner repair and deadline finalization**

On each 30-second plan cycle, process a bounded set of non-terminal latest components:

- complete any component with qualifying durable head evidence;
- finalize expired unresolved components with the explicit partial reason;
- rebind unexpired consumers from a terminal/incompatible scan to the BVID's active scan or create the next scan when none exists;
- repair a missing numbered task for a planned/paused owner using its deterministic slice key.

This path remains inert for shadow cohorts and does not activate live service ownership.

- [ ] **Step 6: Implement terminal worker cleanup**

When task retries exhaust, terminalize the active latest scan, CAS-clear only the matching frontier owner, synchronize all linked components, and preserve failed coverage with the task scan ID. Keep the C4 hot failure path unchanged.

- [ ] **Step 7: Run lifecycle verification**

```powershell
uv run pytest tests/test_worker_cohort_lifecycle.py tests/test_snapshot_cohort_planner.py tests/test_latest_scan_worker.py tests/test_cohort_materialization.py -q
uv run ruff check books_of_time/db/cohort_repositories.py books_of_time/task_orchestrator/snapshot_cohort_planner.py books_of_time/worker.py tests/test_worker_cohort_lifecycle.py tests/test_snapshot_cohort_planner.py
```

- [ ] **Step 8: Commit the cohort lifecycle unit**

```powershell
git add books_of_time/db/cohort_repositories.py books_of_time/task_orchestrator/snapshot_cohort_planner.py books_of_time/worker.py tests/test_worker_cohort_lifecycle.py tests/test_snapshot_cohort_planner.py tests/test_latest_scan_worker.py
git commit -m "feat(cohorts): resolve shared latest scans"
```

---

### Task 9: C5 Documentation, PostgreSQL Acceptance, And Audit

**Files:**
- Modify: `config/config.yaml.example`
- Modify: `docs/CONFIGURATION.md`
- Modify: `docs/COLLECTION.md`
- Modify: `docs/DATA_MODEL.md`
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/TODO.md`
- Modify: `docs/superpowers/plans/2026-07-14-latest-scan-baseline-c5.md`

**Interfaces:**
- Documents legacy versus scan-backed latest behavior, active ownership, CAS, anchors, automatic baseline, slice timing, cohort joining, failure/deadline semantics, and C5/C6/C7 boundaries.
- Marks only C5 complete; C6-C9 remain unchecked.

- [ ] **Step 1: Run opt-in PostgreSQL tests**

Use the configured PostgreSQL URL only through `BOT_TEST_POSTGRESQL_URL`; tests create and remove an isolated schema. Run both latest active-claim concurrency and the existing hot slice race. Never run migration downgrade against the configured schema.

- [ ] **Step 2: Run migration and focused C5 verification**

```powershell
uv run pytest tests/test_schema_migrations.py::test_latest_comment_scan_revision_round_trip -q
uv run pytest tests/test_latest_frontier_policy.py tests/test_comment_scan_models.py tests/test_latest_scan_repositories.py tests/test_snapshot_cohort_planner.py tests/test_cohort_materialization.py tests/test_latest_scan_worker.py tests/test_latest_comments_worker.py tests/test_worker_cohort_lifecycle.py -q
```

- [ ] **Step 3: Document the complete flow**

Document:

```text
single active latest scan per BVID
frontier version CAS and stale-worker rejection
up to five ordered anchors and primary compatibility field
automatic tail -> child head sweep, including empty frontier
incremental reached/missing/corrupted outcomes
dynamic 10-55 second numbered slices
all-status slice identity and restart behavior
cohort joined_active_task and current-head capture window
raw/page/comment/coverage scan evidence
legacy manual latest path remains compatible
C6 owns full/segmented reconciliation
C7 owns live activation, legacy draining/mapping, short page transactions, and lease renewal
PostgreSQL multi-worker versus SQLite single-process expectations
```

- [ ] **Step 4: Perform main-thread P0/P1 audit**

Review active partial-index coverage, claim races, stale CAS, tail child duplication, empty frontier, primary-anchor deletion resilience, retry persistence, cursor-loop terminalization, official frontier mutation only on terminal outcomes, current-head window boundaries, fan-out across multiple cohorts, deadline finalization, terminal worker cleanup, legacy task compatibility, shadow no-run/no-task, and scan evidence propagation.

- [ ] **Step 5: Run complete verification**

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
git diff --check
```

Expected: full suite passes, Ruff is clean, every Python file is formatted, and no whitespace errors exist.

- [ ] **Step 6: Mark C5 complete and C6 next**

Change C5 to `[x]`, leave C6-C9 unchecked, and update Near-term Sprint to identify C6 Visibility And Reconciliation as the next stage. Do not mark the overall Collection-First Snapshot Cohorts mainline complete or enable live rollout.

- [ ] **Step 7: Commit documentation and completion state**

```powershell
git add config/config.yaml.example docs/CONFIGURATION.md docs/COLLECTION.md docs/DATA_MODEL.md docs/OPERATIONS.md docs/TODO.md docs/superpowers/plans/2026-07-14-latest-scan-baseline-c5.md
git commit -m "docs: complete latest scan baseline C5"
```

## Plan Self-Review Result

- **Spec coverage:** Tasks 1-8 cover ordered anchors, active uniqueness, CAS, dynamic slicing, automatic tail/head baseline, incremental continuation, joined cohort consumers, current-head windows, restart repair, and terminal failures. Task 9 covers PostgreSQL concurrency, migration, audit, docs, and acceptance. C6 retains full/segmented reconciliation and visibility; C7 retains live owner transfer, legacy task migration, capacity/storage gates, page-level transaction splitting, and lease renewal.
- **Placeholder scan:** No TBD, generic “handle errors,” unnamed test, or unresolved implementation choice remains. Each task defines exact files, interfaces, RED/GREEN commands, and a commit boundary.
- **Type consistency:** `FrontierStateUpdate`, `FrontierVersionConflict`, `LatestScanRunPlan`, `LatestScanClaim`, anchor helpers, scan modes, slice fields, frontier columns, and component links use the same names throughout.
- **Execution choice:** The user authorized autonomous inline execution on `main` and requested conservative subagent use. Execute in the main thread with TDD; do not stop for design approval and do not create a worktree.
