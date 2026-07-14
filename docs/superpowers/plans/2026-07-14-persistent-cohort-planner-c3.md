# Persistent Cohort Planner C3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the persistent 30-second snapshot cohort planner, atomic cohort/component/task materialization, restart recovery, and worker evidence linkage while exposing it to the service only as a no-request shadow planner.

**Architecture:** C3 adds a deterministic planner above the C2 policy/state model and a transaction-local materializer below it. The materializer supports both shadow and live plans so its idempotency and worker contracts can be verified now, but `build_default_scheduled_jobs` rejects live rollout until C7 transfers ownership away from the legacy video sweep and recursive collector scheduling.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async ORM, Alembic, PostgreSQL row locks and uniqueness constraints, SQLite isolated migration/repository tests, pytest, Ruff.

## Global Constraints

- The approved design is `docs/superpowers/specs/2026-07-13-collection-snapshot-cohorts-design.md`; C3 implements only its persistent planner, recovery, task linkage, and shadow rollout boundary.
- The configured policy identity defaults to `cohort-default-v1`; the rollout mode defaults to `shadow`.
- `snapshot_cohorts.enabled=false` remains the example default. Enabling C3 creates one 30-second scheduled planner job only in shadow mode.
- The service must reject `rollout_mode=live` during C3. Live ownership migration remains C7.
- Existing `video_snapshot_sweep`, daily terminal scheduling, and collector recursive scheduling remain the only live routine owners in C3.
- Shadow cohorts persist planning evidence with parent status `shadow_planned` and create no `collection_tasks`.
- The lower-level materializer may create live tasks in direct repository tests; that API is intentionally unreachable from normal service configuration until C7.
- A cohort, its missing components, and its initial tasks are flushed in one caller-owned transaction under the per-video collection-state lock.
- Stable cohort/component keys come from C2. Repeated planning must not create duplicate cohorts, components, or initial tasks even after a prior task becomes terminal.
- Mandatory checkpoints use the immutable publish anchor at T+6/12/18/24h and a 60-minute start deadline by default.
- Missing checkpoints inside the lateness bound are recreated. Older checkpoints are represented as missed/not-applicable evidence and collapsed into one current recovery cohort per latest overdue checkpoint.
- Stale routine work is never sent as historical work. The planner records a schedule gap and creates at most one current routine observation.
- C3 component mappings are deliberately minimal: `video_metrics`, `hot_core`, `latest_current_head`, and `latest_reconciliation`; hot work is one page until C4 and latest scan-run correctness remains C5.
- Coverage rows and HTTP request attempts copy cohort/component IDs from their owning task. Secrets remain excluded.
- All persisted datetimes are UTC-aware. The planner accepts an explicit `now` and contains no direct wall-clock reads.
- Public user identifiers remain visible. Media stays on the local filesystem. No S3/OSS/MinIO dependency is introduced.
- Never downgrade the configured user PostgreSQL database. Migration cycles use isolated SQLite files.
- Use TDD for every production behavior, one Conventional Commit per implementation task, and update this plan's checkboxes in the corresponding commit.

## File Map

- `books_of_time/domain/enums.py`: add the persistent planner scheduled-job kind.
- `books_of_time/domain/cohort_policy.py`: add policy version and rollout-mode configuration plus deterministic serialization.
- `alembic/versions/0010_snapshot_cohort_planning_job.py`: extend the PostgreSQL scheduled-job enum and remove planner rows on downgrade.
- `books_of_time/db/cohort_repositories.py`: add configured-policy bootstrap, materialization, gap, deadline, and component lifecycle repositories.
- `books_of_time/db/repositories.py`: accept task cohort links and propagate them into coverage/HTTP-attempt evidence.
- `books_of_time/task_orchestrator/snapshot_cohort_planner.py`: deterministic due/checkpoint/recovery planner and task specifications.
- `books_of_time/service/scheduled_jobs.py`: register the optional 30-second shadow planner and reject live service rollout.
- `books_of_time/worker.py`: start/finalize cohort components with task execution.
- `config/config.yaml.example`: document disabled shadow defaults and policy identity.
- `docs/CONFIGURATION.md`, `docs/COLLECTION.md`, `docs/DATA_MODEL.md`, `docs/OPERATIONS.md`: document the C3 runtime and ownership boundary.
- `docs/TODO.md`: mark C3 in progress at plan start and complete only after full verification.
- `tests/test_cohort_policy_config.py`: rollout/policy identity validation.
- `tests/test_schema_migrations.py`: static and isolated `0010` migration checks.
- `tests/test_cohort_materialization.py`: atomic/idempotent shadow and live materialization.
- `tests/test_snapshot_cohort_planner.py`: deterministic routine/checkpoint/recovery/gap behavior.
- `tests/test_worker_cohort_lifecycle.py`: component/cohort transitions and evidence linkage.
- `tests/test_service_scheduled_handlers.py`: optional 30-second shadow service integration and no-task proof.
- `tests/test_http_request_attempts.py`, `tests/test_coverage_repositories.py`: direct cohort-link propagation coverage.

---

### Task 1: Add Planner Job And Rollout Configuration

**Files:**
- Modify: `books_of_time/domain/enums.py`
- Modify: `books_of_time/domain/cohort_policy.py`
- Create: `alembic/versions/0010_snapshot_cohort_planning_job.py`
- Modify: `config/config.yaml.example`
- Modify: `tests/test_cohort_policy_config.py`
- Modify: `tests/test_schema_migrations.py`

**Interfaces:**
- Produces `ScheduledJobKind.SNAPSHOT_COHORT_PLANNING` with value `snapshot_cohort_planning`.
- Produces `CohortRolloutMode` values `shadow` and `live`.
- Extends `CohortPolicy` with `policy_version: str` and `rollout_mode: CohortRolloutMode`.
- Produces `CohortPolicy.as_persisted_policy() -> dict[str, Any]`, a stable JSON-compatible representation used to enforce immutable policy-version identity.

- [ ] **Step 1: Write failing enum, policy, and migration tests**

Add assertions:

```python
policy = CohortPolicy.from_config(None)
assert policy.policy_version == "cohort-default-v1"
assert policy.rollout_mode is CohortRolloutMode.SHADOW
assert policy.as_persisted_policy()["checkpoint_hours"] == [6, 12, 18, 24]
assert policy.as_persisted_policy()["tier_intervals_minutes"]["s"] == {
    "active": 2,
    "normal": 10,
}
```

Reject empty/whitespace `policy_version`, non-string rollout values, and rollout values outside `shadow|live`. Assert the example YAML parses to disabled shadow mode.

Extend migration tests:

```python
assert get_expected_schema_revision() == "0010_snapshot_cohort_planning_job"
assert "ADD VALUE IF NOT EXISTS 'snapshot_cohort_planning'" in source
assert "DELETE FROM scheduled_jobs" in source
```

The isolated cycle upgrades to head, inserts no planner data, downgrades to `0009_cohort_state_and_policy`, and upgrades to head again.

- [ ] **Step 2: Run focused tests to verify RED**

```powershell
uv run pytest tests/test_cohort_policy_config.py tests/test_schema_migrations.py -q
```

Expected: enum/import/default/head assertions fail because the C3 contracts do not exist.

- [ ] **Step 3: Implement rollout configuration and stable serialization**

Add:

```python
class CohortRolloutMode(StrEnum):
    SHADOW = "shadow"
    LIVE = "live"
```

Parse exact top-level `snapshot_cohorts.policy_version` and `rollout_mode`. `as_persisted_policy` must return newly allocated dictionaries/lists containing every C2 field that affects planning: timezone, checkpoint hours/lateness, downgrade settings, tier thresholds, lifecycle intervals, activity windows, and tier intervals. It excludes operational enablement and rollout mode because those control execution, not policy evidence.

- [ ] **Step 4: Add static migration and example configuration**

Create revision `0010_snapshot_cohort_planning_job` with `down_revision="0009_cohort_state_and_policy"`. On PostgreSQL, use an Alembic autocommit block to add `snapshot_cohort_planning` to `scheduledjobkind`. Downgrade deletes rows with that kind and intentionally retains the PostgreSQL enum value.

Set the example shape:

```yaml
snapshot_cohorts:
  # C3 may be enabled only in shadow mode; live ownership moves in C7.
  enabled: false
  policy_version: cohort-default-v1
  rollout_mode: shadow
  planning_seconds: 30
```

- [ ] **Step 5: Verify GREEN**

```powershell
uv run pytest tests/test_cohort_policy_config.py tests/test_schema_migrations.py -q
uv run ruff check books_of_time/domain/enums.py books_of_time/domain/cohort_policy.py alembic/versions/0010_snapshot_cohort_planning_job.py tests/test_cohort_policy_config.py tests/test_schema_migrations.py
```

- [ ] **Step 6: Commit**

```powershell
git add books_of_time/domain/enums.py books_of_time/domain/cohort_policy.py alembic/versions/0010_snapshot_cohort_planning_job.py config/config.yaml.example tests/test_cohort_policy_config.py tests/test_schema_migrations.py docs/superpowers/plans/2026-07-14-persistent-cohort-planner-c3.md
git commit -m "feat(planner): add cohort rollout configuration"
```

---

### Task 2: Materialize Cohorts, Components, And Initial Tasks Atomically

**Files:**
- Modify: `books_of_time/db/cohort_repositories.py`
- Modify: `books_of_time/db/repositories.py`
- Create: `tests/test_cohort_materialization.py`
- Modify: `tests/test_task_queue.py`

**Interfaces:**
- Produces frozen `CohortComponentPlan` and `SnapshotCohortPlan` value objects in `books_of_time.db.cohort_repositories`.
- Produces `CohortMaterializationResult` with cohort, component, and task creation counts.
- Produces `SnapshotCohortRepository.materialize(plan, *, rollout_mode, now) -> CohortMaterializationResult`.
- Extends `CollectionTaskRepository.enqueue(..., snapshot_cohort_id: int | None = None, snapshot_cohort_component_id: int | None = None)`.

- [ ] **Step 1: Write failing materialization tests**

Use a real in-memory ORM graph with one policy version, known video, and adopted state. Build this exact routine plan:

```python
SnapshotCohortPlan(
    cohort_key="snapshot:BV-C3:2026-07-14T04:00:00Z:routine",
    bvid="BV-C3",
    scheduled_for=now,
    reason="routine",
    age_checkpoint_hours=None,
    desired_tier=CollectionTier.S,
    effective_tier=CollectionTier.S,
    policy_version="cohort-default-v1",
    deadline=now + timedelta(minutes=2),
    status=CohortStatus.PLANNED,
    status_reason=None,
    extra={"planner_bucket_seconds": 30},
    components=(
        CohortComponentPlan("video_metrics", TaskKind.FETCH_VIDEO_STATS, 1),
        CohortComponentPlan("hot_core", TaskKind.FETCH_HOT_COMMENTS, 1),
        CohortComponentPlan(
            "latest_current_head", TaskKind.FETCH_LATEST_COMMENTS, 1
        ),
    ),
)
```

Assert:

1. Shadow materialization creates one `shadow_planned` cohort and three pending components, but zero tasks.
2. Repeating the same shadow plan returns the same IDs and creates nothing.
3. Live materialization in a separate graph creates one task per pending component with both cohort IDs populated and the stable component key as idempotency key.
4. Repeating live materialization after marking tasks succeeded still creates no replacement initial tasks.
5. Reusing a cohort key with a different BVID, schedule time, checkpoint age, or policy version raises `ValueError` and leaves the transaction graph unchanged after rollback.
6. A recovery plan with the same stable key may add a newly missing component, updating `expected_component_count`, but cannot mutate existing component identity or planned pages.
7. Repository methods flush only; caller rollback removes the whole graph.

- [ ] **Step 2: Run materialization tests to verify RED**

```powershell
uv run pytest tests/test_cohort_materialization.py tests/test_task_queue.py -q
```

Expected: plan value objects, materializer, and enqueue link parameters are missing.

- [ ] **Step 3: Implement caller-owned atomic materialization**

`materialize` must:

1. lock `VideoCollectionState` for the BVID with `FOR UPDATE` where supported;
2. load or insert the cohort by `cohort_key`;
3. validate immutable identity when the cohort already exists;
4. load or insert each component under `(cohort_id, component_kind)`;
5. update only the recovery cohort's missing-component union and parent expected count;
6. create at most one initial task for each pending component after checking every task status by component ID, not only the active-idempotency index;
7. set parent status to `shadow_planned` and skip task creation in shadow mode;
8. flush without commit so the scheduled-job coordinator owns the transaction.

Use nested savepoints around first inserts so unique-constraint races can be reloaded without aborting the outer transaction. The per-video state lock serializes normal PostgreSQL planners; uniqueness remains the final authority. SQLite is documented as a single-process development target.

Task payload is copied from the component plan and augmented with `bvid`, `reason`, `scheduled_for`, `cohort_key`, and `component_kind`. Do not store cookies, authenticated URLs, or headers.

- [ ] **Step 4: Verify GREEN**

```powershell
uv run pytest tests/test_cohort_materialization.py tests/test_task_queue.py tests/test_cohort_repositories.py -q
uv run ruff check books_of_time/db/cohort_repositories.py books_of_time/db/repositories.py tests/test_cohort_materialization.py tests/test_task_queue.py
```

- [ ] **Step 5: Commit**

```powershell
git add books_of_time/db/cohort_repositories.py books_of_time/db/repositories.py tests/test_cohort_materialization.py tests/test_task_queue.py docs/superpowers/plans/2026-07-14-persistent-cohort-planner-c3.md
git commit -m "feat(planner): materialize cohort task graphs"
```

---

### Task 3: Plan Routine, Checkpoint, Recovery, And Schedule-Gap Evidence

**Files:**
- Create: `books_of_time/task_orchestrator/snapshot_cohort_planner.py`
- Modify: `books_of_time/db/cohort_repositories.py`
- Create: `tests/test_snapshot_cohort_planner.py`

**Interfaces:**
- Produces frozen `CohortPlanningSummary` counters.
- Produces `SnapshotCohortPlanner(policy: CohortPolicy, *, batch_limit: int = 5000)`.
- Produces `SnapshotCohortPlanner.plan_due(session, *, now, rollout_mode=None) -> CohortPlanningSummary`.
- Produces `CollectionPolicyVersionRepository.ensure_configured(policy, *, now) -> CollectionPolicyVersion`.
- Produces `VideoCollectionStateRepository.list_candidates(limit)`, `lock(bvid)`, and `record_planning(...)`.
- Produces `CollectionScheduleGapRepository.record(...)` with stable identity.

- [ ] **Step 1: Write failing deterministic planner tests**

Use explicit UTC clocks and real ORM rows. Cover these independent cases:

1. First sight of a known video creates/activates immutable `cohort-default-v1`, adopts the video with its pubdate anchor, and plans one current routine cohort without historical routine backfill.
2. A monitored official video at age 5h59m assesses S; the same video first discovered at age 8h does not receive initial-age S.
3. A due T+6h checkpoint creates `video_metrics`, `hot_core`, and `latest_reconciliation`, uses deadline `scheduled_for + 60m`, and coalesces a routine in the same 30-second bucket.
4. A checkpoint missing at T+6h30m is recreated as a real checkpoint; at T+7h01m it is represented as missed and one current recovery cohort contains only components without usable evidence.
5. A checkpoint earlier than `KnownVideo.first_seen_at` is `not_applicable_before_discovery` and never enters recovery.
6. Repeated overdue planning keeps one recovery key `snapshot:{bvid}:recovery:through:{hours}h`; a later overdue checkpoint advances the key and unions still-missing component kinds once.
7. A stale `next_due_at` creates one `collection_schedule_gaps` row and one current routine cohort, never a task per expired slot.
8. Repeating the same planner cycle changes no row counts.
9. An archived video plans only `video_metrics`; a dormant video includes latest only when a complete frontier exists.
10. Shadow execution produces identical cohort/component decisions to live planning but creates no tasks.

- [ ] **Step 2: Run planner tests to verify RED**

```powershell
uv run pytest tests/test_snapshot_cohort_planner.py -q
```

Expected: planner/repository APIs are absent.

- [ ] **Step 3: Bootstrap immutable configured policy evidence**

`ensure_configured` compares `policy.as_persisted_policy()` with any existing row of the same version. It creates the row with:

```text
policy_kind=snapshot_cohort
scope_type=global
scope_id=global
timezone=<policy.timezone.key>
algorithm=configured-fixed-v1
```

If the same version already stores different policy content, raise a clear `ValueError` instructing the operator to choose a new version. Activate the configured row without editing immutable fields.

- [ ] **Step 4: Implement deterministic planning decisions**

For each candidate video:

1. adopt or lock its state;
2. read monitored-official source evidence, active event membership, latest frontier completeness, and one-hour view growth;
3. apply C2 desired/effective tier and lifecycle functions;
4. evaluate every configured checkpoint against immutable publish time and first-seen time;
5. materialize due checkpoint, missed/not-applicable evidence, and at most one current recovery plan;
6. calculate the current routine interval from age/growth, tier ceiling, activity window, and next future checkpoint;
7. summarize stale routine time as a gap and materialize at most one current routine plan unless coalesced with the checkpoint bucket;
8. persist `desired_tier`, `effective_tier`, downgrade state, lifecycle, policy version, `next_due_at`, `last_planned_at`, and `last_checkpoint_hours`.

Use 30-second UTC floor buckets for planner-cycle collision only; checkpoint schedule times retain their exact publish-anchor timestamp. For C3, all executable hot/latest component plans use one page. Priorities are deterministic: checkpoint core 120, recovery 115, routine S/A/B/C 100/90/80/70, with video metrics before hot and latest at equal cohort class.

Missing overdue live components finalize as `missed_due_to_service_gap` when the checkpoint was never planned and `missed_due_to_capacity` when a planned pending component passed its deadline. Shadow parent rows remain `shadow_planned`; their `extra.shadow_target_status` records the simulated live parent status.

- [ ] **Step 5: Verify planner GREEN and 48-hour deterministic simulation**

```powershell
uv run pytest tests/test_snapshot_cohort_planner.py tests/test_cohort_time_policy.py tests/test_cohort_tier_policy.py tests/test_cohort_lifecycle_policy.py -q
uv run ruff check books_of_time/task_orchestrator/snapshot_cohort_planner.py books_of_time/db/cohort_repositories.py tests/test_snapshot_cohort_planner.py
```

The test simulation advances an explicit clock in 30-second steps across T+0/30m/6h/12h/18h/24h and asserts stable keys, no duplicate rows, checkpoint/routine collision, and restart gap reconstruction.

- [ ] **Step 6: Commit**

```powershell
git add books_of_time/task_orchestrator/snapshot_cohort_planner.py books_of_time/db/cohort_repositories.py tests/test_snapshot_cohort_planner.py docs/superpowers/plans/2026-07-14-persistent-cohort-planner-c3.md
git commit -m "feat(planner): plan persistent snapshot cohorts"
```

---

### Task 4: Link Worker Lifecycle, Coverage, And HTTP Attempts

**Files:**
- Modify: `books_of_time/db/cohort_repositories.py`
- Modify: `books_of_time/db/repositories.py`
- Modify: `books_of_time/db/http_evidence.py`
- Modify: `books_of_time/worker.py`
- Create: `tests/test_worker_cohort_lifecycle.py`
- Modify: `tests/test_coverage_repositories.py`
- Modify: `tests/test_http_request_attempts.py`

**Interfaces:**
- Produces `SnapshotCohortExecutionRepository.mark_task_started(task, *, now)`.
- Produces `SnapshotCohortExecutionRepository.record_task_succeeded(task, coverage, *, finished_at)`.
- Produces `SnapshotCohortExecutionRepository.record_task_failed(task, coverage, *, terminal, finished_at)`.
- Coverage and HTTP attempt rows copy `task.snapshot_cohort_id` and `task.snapshot_cohort_component_id`.
- `DatabaseHttpEvidenceSink` accepts nullable cohort/component IDs from the worker.

- [ ] **Step 1: Write failing execution and evidence tests**

Materialize one live `video_metrics` task and run a real `Worker` with a deterministic collector. Assert:

1. Leasing marks component/cohort running, captures first `started_at`, and computes `skew_seconds = task_start - scheduled_for` once.
2. Success copies coverage counters into the component, marks it complete, aggregates the cohort, and updates `completed_component_count`/`finished_at` when all required components finish.
3. Partial and corrupted drafts map to component/cohort partial and corrupted status respectively.
4. A retryable failure keeps the component active and unfinished; exhausted retries mark it failed and aggregate the parent.
5. Missing collector follows the same terminal component-failure path.
6. A standalone legacy task with null cohort IDs behaves exactly as before.
7. `CollectionCoverageStat` and every `HttpRequestAttempt` created under the task carry both IDs.
8. The first request attempt, not task lease acquisition, remains the authoritative network timestamp; no fabricated attempt is created for a collector that never calls HTTP.

- [ ] **Step 2: Run focused tests to verify RED**

```powershell
uv run pytest tests/test_worker_cohort_lifecycle.py tests/test_coverage_repositories.py tests/test_http_request_attempts.py -q
```

Expected: cohort execution APIs and evidence-link assertions fail.

- [ ] **Step 3: Propagate task identity into durable evidence**

Set `snapshot_cohort_id` and `snapshot_cohort_component_id` in both coverage insert paths. Extend `HttpRequestAttemptRepository.begin` and `DatabaseHttpEvidenceSink` with nullable IDs and have the worker pass the leased task's values. Existing direct callers retain defaults of `None`.

- [ ] **Step 4: Implement component/cohort transitions in the worker transaction**

After lease and before collector dispatch, call `mark_task_started`. On every success/failure/no-collector branch, insert coverage first, then update the component from that persisted coverage object, then commit task/run/component/cohort changes together.

Component counters are monotonic sums across task attempts/follow-ups. Completion mapping is exact:

```text
coverage corrupted -> component corrupted
coverage partial   -> component partial
coverage succeeded -> component complete
terminal failure   -> component failed
retry scheduled    -> component running
```

Recompute the parent with C2 `aggregate_cohort_status`; shadow cohorts never reach the worker. Update a video's `last_completed_cohort_at` only for a complete cohort.

- [ ] **Step 5: Verify GREEN**

```powershell
uv run pytest tests/test_worker_cohort_lifecycle.py tests/test_worker_loop.py tests/test_worker_coverage.py tests/test_coverage_repositories.py tests/test_http_request_attempts.py -q
uv run ruff check books_of_time/db/cohort_repositories.py books_of_time/db/repositories.py books_of_time/db/http_evidence.py books_of_time/worker.py tests/test_worker_cohort_lifecycle.py tests/test_coverage_repositories.py tests/test_http_request_attempts.py
```

- [ ] **Step 6: Commit**

```powershell
git add books_of_time/db/cohort_repositories.py books_of_time/db/repositories.py books_of_time/db/http_evidence.py books_of_time/worker.py tests/test_worker_cohort_lifecycle.py tests/test_coverage_repositories.py tests/test_http_request_attempts.py docs/superpowers/plans/2026-07-14-persistent-cohort-planner-c3.md
git commit -m "feat(worker): track cohort component execution"
```

---

### Task 5: Register The Thirty-Second Shadow Scheduled Job

**Files:**
- Modify: `books_of_time/service/scheduled_jobs.py`
- Modify: `tests/test_service_scheduled_handlers.py`
- Modify: `tests/test_service_coordinator.py`

**Interfaces:**
- Produces `SnapshotCohortPlanningScheduleHandler`.
- `build_default_scheduled_jobs` adds `snapshot-cohort-planning` only when `snapshot_cohorts.enabled=true` and rollout is shadow.
- Normal service configuration rejects live rollout before creating any scheduled-job definition.

- [ ] **Step 1: Write failing service-boundary tests**

Assert:

```python
definitions, handlers = build_default_scheduled_jobs(
    {"snapshot_cohorts": {"enabled": True, "rollout_mode": "shadow"}}
)
definition = next(
    item
    for item in definitions
    if item.job_kind is ScheduledJobKind.SNAPSHOT_COHORT_PLANNING
)
assert definition.job_key == "snapshot-cohort-planning"
assert definition.schedule_seconds == 30
assert isinstance(
    handlers[ScheduledJobKind.SNAPSHOT_COHORT_PLANNING],
    SnapshotCohortPlanningScheduleHandler,
)
```

Also assert disabled config adds no planner job; custom positive planning seconds are honored; and enabled live config raises `ValueError` mentioning the C7 ownership migration.

Run the handler/coordinator against one known video. Assert policy/state/cohort/component rows commit, `SnapshotCohort.status == "shadow_planned"`, and `CollectionTask` count remains zero. Use collectors/HTTP fakes that fail the test if called, proving shadow planning performs no platform request.

- [ ] **Step 2: Run service tests to verify RED**

```powershell
uv run pytest tests/test_service_scheduled_handlers.py tests/test_service_coordinator.py -q
```

Expected: no planner definition/handler exists and live rollout is not rejected.

- [ ] **Step 3: Implement shadow-only service registration**

Parse `CohortPolicy` once in `build_default_scheduled_jobs`. When disabled, preserve the existing definition set exactly. When enabled in shadow mode, register priority 110 and schedule `policy.planning_seconds`; the handler calls `planner.plan_due(session, now=now, rollout_mode=SHADOW)` and logs summary counts.

When enabled with live mode, raise before returning definitions:

```text
snapshot_cohorts.rollout_mode=live is unavailable until C7 migrates all routine scheduling ownership
```

Do not disable, delegate, or edit the legacy video sweep, terminal handler, or collector recursion in C3.

- [ ] **Step 4: Verify GREEN and explicit no-request proof**

```powershell
uv run pytest tests/test_service_scheduled_handlers.py tests/test_service_coordinator.py tests/test_snapshot_cohort_planner.py -q
uv run ruff check books_of_time/service/scheduled_jobs.py tests/test_service_scheduled_handlers.py tests/test_service_coordinator.py
```

- [ ] **Step 5: Commit**

```powershell
git add books_of_time/service/scheduled_jobs.py tests/test_service_scheduled_handlers.py tests/test_service_coordinator.py docs/superpowers/plans/2026-07-14-persistent-cohort-planner-c3.md
git commit -m "feat(service): run cohort planner in shadow mode"
```

---

### Task 6: Document, Verify, And Audit C3

**Files:**
- Modify: `docs/CONFIGURATION.md`
- Modify: `docs/COLLECTION.md`
- Modify: `docs/DATA_MODEL.md`
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/TODO.md`
- Modify: `docs/superpowers/plans/2026-07-14-persistent-cohort-planner-c3.md`

**Interfaces:**
- Documents operator configuration, planner transactions, persisted evidence, restart behavior, and the C3/C7 ownership boundary.
- Marks only C3 complete; C4-C9 remain unchecked.

- [ ] **Step 1: Document the complete C3 operator and data flow**

Document:

- how to enable `snapshot_cohorts.enabled=true` with `rollout_mode=shadow`;
- why `policy_version` must change when planning policy content changes;
- the 30-second job, UTC buckets, immutable publish anchor, checkpoint deadlines, recovery keys, and schedule gaps;
- the exact shadow guarantee: cohort/component/state/policy evidence is written, no executable task or HTTP request is created;
- lower-level live task linkage and worker status semantics as implemented but not service reachable;
- existing live scheduler ownership remains unchanged until C7;
- PostgreSQL multi-scheduler expectations and SQLite single-process limits;
- Docker/external PostgreSQL, Linux native, and Windows native behavior remains identical.

- [ ] **Step 2: Run focused migration and planner verification**

```powershell
uv run pytest tests/test_schema_migrations.py::test_snapshot_cohort_planning_job_revision_round_trip -q
uv run pytest tests/test_cohort_materialization.py tests/test_snapshot_cohort_planner.py tests/test_worker_cohort_lifecycle.py tests/test_service_scheduled_handlers.py -q
```

- [ ] **Step 3: Run complete verification**

```powershell
uv run pytest
uv run ruff check .
git diff --check
```

Expected: all tests pass, Ruff is clean, and no whitespace errors exist.

- [ ] **Step 4: Perform a P0/P1 audit**

Review transaction rollback, duplicate initial-task prevention, checkpoint lateness inclusivity, first-seen not-applicable boundaries, shadow no-task enforcement, live service rejection, worker retry transitions, and evidence ID propagation. Any confirmed P0/P1 bug gets its own failing regression test, fix commit, and `docs/fix/2026-07-14_<no>.md` record with code location, reason, expected result, approach, introducing commit hash, and fixing commit hash. Do not refactor speculative or over-designed surfaces.

- [ ] **Step 5: Mark C3 complete and C4 next**

Change C3 from `[~]` to `[x]`, keep C4-C9 unchecked, and update Near-term Sprint to say C1-C3 complete and C4 Hot Core And Deep Scans next. Do not mark the overall Collection-First Snapshot Cohorts mainline complete.

- [ ] **Step 6: Commit documentation and completion state**

```powershell
git add docs/CONFIGURATION.md docs/COLLECTION.md docs/DATA_MODEL.md docs/OPERATIONS.md docs/TODO.md docs/superpowers/plans/2026-07-14-persistent-cohort-planner-c3.md
git commit -m "docs: complete persistent cohort planner C3"
```

## Plan Self-Review Result

- **Spec coverage:** C3 covers the 30-second persistent job, configured immutable policy identity, per-video adoption/locking, deterministic routine/checkpoint/recovery/gap planning, atomic cohort/component/initial-task materialization, worker/coverage/attempt linkage, and shadow rollout. C4 owns multi-page hot/deep slicing; C5 owns scan runs/CAS/latest baseline correctness; C6 owns visibility/reconciliation; C7 owns capacity/fairness/storage gates and live ownership transfer; C8 owns learned windows; C9 owns live acceptance/integrity audits.
- **Placeholder scan:** No TBD, unnamed error handling, or deferred implementation instruction remains. Every task names files, interfaces, RED/GREEN commands, transaction rules, and commit boundaries.
- **Type consistency:** `CohortPolicy`, `CohortRolloutMode`, plan value objects, materialization result, planner summary, repository methods, worker hooks, and scheduled handler names are defined once and reused consistently.
- **Execution choice:** The user authorized autonomous inline execution and asked to avoid aggressive subagent creation. Execute this plan in the main thread with TDD and one commit per task.
