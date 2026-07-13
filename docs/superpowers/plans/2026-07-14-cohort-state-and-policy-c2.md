# Cohort State And Policy C2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the persistent cohort/policy state and deterministic pure policy functions that C3 can consume without scheduling or executing any platform request.

**Architecture:** Add the C2 tables and nullable task/coverage links through one reversible Alembic revision, then implement immutable configuration value objects and pure UTC/tier/lifecycle/status functions in a focused domain module. Add only policy activation and known-video adoption repositories; executable cohort planning, task ownership, scan runs, and worker integration remain in C3-C7.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async ORM, Alembic, PostgreSQL production constraints, SQLite isolated migration tests, `zoneinfo`, pytest, Ruff.

## Global Constraints

- Preserve every C1 evidence field and migration; revision `0009` extends `0008_collection_evidence_foundations`.
- All persisted datetimes are UTC-aware; activity-window evaluation uses the configured IANA timezone, default `Asia/Shanghai`.
- Tiers are exactly `s`, `a`, `b`, `c`; lifecycle stages are exactly `active`, `dormant`, `archived`.
- Checkpoints are exactly configurable strictly increasing positive hours, with defaults `6/12/18/24`.
- S ceilings are 2 minutes in an activity window and 10 minutes otherwise; A ceilings are 10/30, B 30/60, C 60/120.
- Numeric tier thresholds use OR semantics and first-match order S, A, B, C. Upgrades are immediate; downgrades require two consecutive assessments by default.
- Official monitored videos use immutable publish age for the initial six-hour S rule; discovery time never restarts that window.
- Model-derived bot, steering, stance, or coordination values are not policy inputs.
- C2 does not enqueue tasks, alter scheduler ownership, or activate a planner. Those changes begin in C3.
- Media remains local. No schema or policy change may introduce S3/OSS media storage.
- Use one Conventional Commit per task and update this plan's checkboxes in the corresponding commit.

## File Map

- `books_of_time/db/models.py`: add C2 ORM tables and nullable cohort links.
- `alembic/versions/0009_cohort_state_and_policy.py`: reversible static schema migration.
- `books_of_time/db/schema.py`: extend exact legacy-adoption allowlists for C2 additions.
- `books_of_time/domain/cohort_policy.py`: enums, immutable config objects, time/tier/lifecycle/status pure functions.
- `books_of_time/db/cohort_repositories.py`: policy activation and known-video adoption only.
- `config/config.yaml.example`: disabled-until-C3 policy configuration with approved defaults.
- `docs/CONFIGURATION.md`, `docs/COLLECTION.md`, `docs/DATA_MODEL.md`: C2 current behavior and explicit non-activation boundary.
- `docs/TODO.md`: mark only C2 complete after full verification.
- `tests/test_cohort_models.py`: metadata constraints and ORM round trips.
- `tests/test_cohort_policy_config.py`: policy parsing and validation.
- `tests/test_cohort_time_policy.py`: time axes, intervals, checkpoints, keys.
- `tests/test_cohort_tier_policy.py`: tier OR semantics and downgrade hysteresis.
- `tests/test_cohort_lifecycle_policy.py`: lifecycle, component eligibility, status aggregation.
- `tests/test_cohort_repositories.py`: immutable policy versions, rollback activation, state adoption.
- `tests/test_schema_migrations.py`: isolated `0009` upgrade/downgrade/upgrade.

---

### Task 1: Add Cohort State Schema

**Files:**
- Modify: `books_of_time/db/models.py`
- Create: `alembic/versions/0009_cohort_state_and_policy.py`
- Modify: `books_of_time/db/schema.py`
- Create: `tests/test_cohort_models.py`
- Modify: `tests/test_schema_migrations.py`

**Interfaces:**
- Produces ORM classes `CollectionPolicyVersion`, `VideoCollectionState`, `SnapshotCohort`, `SnapshotCohortComponent`, and `CollectionScheduleGap`.
- Produces nullable `snapshot_cohort_id` and `snapshot_cohort_component_id` on `CollectionTask` and `CollectionCoverageStat`.
- Consumes existing `KnownVideo`, `CollectionTask`, `CollectionCoverageStat`, `UTCDateTime`, `json_dict_type`, and `bigint_pk_type`.

- [ ] **Step 1: Write failing metadata and constraint tests**

Create `tests/test_cohort_models.py`. Build an in-memory schema and assert these exact contracts:

```python
assert CollectionPolicyVersion.__table__.c.version.unique is True
assert VideoCollectionState.__table__.c.bvid.primary_key is True
assert SnapshotCohort.__table__.c.cohort_key.unique is True
assert SnapshotCohortComponent.__table__.c.cohort_id.nullable is False
assert CollectionScheduleGap.__table__.c.expected_cohort_count.nullable is False
assert CollectionTask.__table__.c.snapshot_cohort_id.nullable is True
assert CollectionCoverageStat.__table__.c.snapshot_cohort_component_id.nullable is True
```

Insert one complete graph using version `cohort-default-v1`, one known video, one state, one checkpoint cohort, one `video_metrics` component, and one gap. Assert defaults, UTC round trip, JSON round trip, and that duplicate `(cohort_id, component_kind)` and duplicate `cohort_key` raise `IntegrityError`.

- [ ] **Step 2: Run model tests to verify RED**

```powershell
uv run pytest tests/test_cohort_models.py -q
```

Expected: import fails because the five C2 ORM classes do not exist.

- [ ] **Step 3: Add exact ORM contracts**

Add models with these persisted fields:

```text
collection_policy_versions:
  id, version UNIQUE, policy_kind, scope_type, scope_id, timezone, policy JSON,
  training_window_start/end, distinct_comment_count, complete_day_count,
  valid_exposure_minutes, excluded_comment_count, exclusion_reasons JSON,
  algorithm, created_at, activated_at, superseded_at, active

video_collection_states:
  bvid PK/FK, desired_tier, effective_tier, candidate_downgrade_tier,
  consecutive_downgrade_count, pinned_tier, life_stage, schedule_anchor_at,
  next_due_at, last_planned_at, last_completed_cohort_at,
  last_checkpoint_hours, policy_version, extra JSON, created_at, updated_at

snapshot_cohorts:
  id, cohort_key UNIQUE, bvid FK, scheduled_for, reason,
  age_checkpoint_hours, desired_tier, effective_tier, policy_version,
  deadline, status, status_reason, started_at, finished_at,
  expected_component_count, completed_component_count, extra JSON,
  created_at, updated_at

snapshot_cohort_components:
  id, cohort_id FK CASCADE, component_kind, required, status, scheduled_for,
  deadline, started_at, finished_at, skew_seconds, planned_pages,
  requested_pages, succeeded_pages, items_observed, raw_payloads_saved,
  comment_scan_run_id NULL, failure_reason, extra JSON,
  UNIQUE(cohort_id, component_kind)

collection_schedule_gaps:
  id, bvid FK, gap_start, gap_end, expected_cohort_count, reason,
  service_instance_id, policy_version, created_at,
  UNIQUE(bvid, gap_start, gap_end, reason, policy_version)
```

Use string columns plus `CheckConstraint` for tier, lifecycle, cohort status, component status, non-negative counters, and `gap_end > gap_start`. Add indexes for active policy scope, video next due/life stage, cohort BVID/time and status/deadline, component status/deadline, and gap BVID/time. The active policy index is partial and unique for `(policy_kind, scope_type, scope_id)` where `active=true` on both PostgreSQL and SQLite.

Task and coverage cohort IDs are logical nullable references in C2, matching the repository's existing high-write reference style. C3 owns task creation; C5 adds scan-run/slice fields.

- [ ] **Step 4: Add static Alembic revision and isolated round trip**

Create `0009_cohort_state_and_policy` with `down_revision="0008_collection_evidence_foundations"`. The migration must use explicit `op.create_table`, `op.add_column`, `op.create_index`, `op.drop_index`, `op.drop_column`, and `op.drop_table`; it must not import `Base.metadata`.

Extend `tests/test_schema_migrations.py`:

```python
def test_cohort_state_revision_round_trip(tmp_path: Path) -> None:
    # isolated SQLite file only
    command.upgrade(config, "head")
    assert _sqlite_table_exists(path, "snapshot_cohorts")
    assert "snapshot_cohort_id" in _sqlite_columns(path, "collection_tasks")
    command.downgrade(config, "0008_collection_evidence_foundations")
    assert not _sqlite_table_exists(path, "snapshot_cohorts")
    assert "snapshot_cohort_id" not in _sqlite_columns(path, "collection_tasks")
    command.upgrade(config, "head")
```

Update legacy adoption's exact table/column allowlist so only these known C2 additions may be absent. Never run downgrade against the configured user PostgreSQL database.

- [ ] **Step 5: Verify schema GREEN**

```powershell
uv run pytest tests/test_cohort_models.py tests/test_schema_migrations.py -q
uv run ruff check books_of_time/db/models.py books_of_time/db/schema.py tests/test_cohort_models.py tests/test_schema_migrations.py alembic/versions/0009_cohort_state_and_policy.py
```

Expected: model constraints and isolated migration round trip pass.

- [ ] **Step 6: Commit**

```powershell
git add books_of_time/db/models.py books_of_time/db/schema.py alembic/versions/0009_cohort_state_and_policy.py tests/test_cohort_models.py tests/test_schema_migrations.py docs/superpowers/plans/2026-07-14-cohort-state-and-policy-c2.md
git commit -m "feat(db): add cohort state and policy schema"
```

---

### Task 2: Parse And Validate Cohort Policy Configuration

**Files:**
- Create: `books_of_time/domain/cohort_policy.py`
- Create: `tests/test_cohort_policy_config.py`
- Modify: `config/config.yaml.example`

**Interfaces:**
- Produces enums `CollectionTier`, `VideoLifeStage`, `CohortStatus`, and `CohortComponentStatus`.
- Produces frozen value objects `TierThreshold`, `TierInterval`, `ActivityWindow`, `LifecyclePolicy`, and `CohortPolicy`.
- Produces `CohortPolicy.from_config(config: Mapping[str, Any] | None) -> CohortPolicy`.

- [ ] **Step 1: Write failing default and validation tests**

Test exact defaults from the approved spec:

```python
policy = CohortPolicy.from_config(None)
assert policy.timezone.key == "Asia/Shanghai"
assert policy.checkpoint_hours == (6, 12, 18, 24)
assert policy.downgrade_confirmations == 2
assert policy.tier_intervals[CollectionTier.S].active == timedelta(minutes=2)
assert policy.tier_intervals[CollectionTier.A].normal == timedelta(minutes=30)
assert policy.lifecycle.dormant_after == timedelta(days=7)
assert policy.lifecycle.archive_after == timedelta(days=30)
```

Parametrize invalid configurations and assert precise `ValueError` messages:

- unknown/invalid IANA timezone;
- checkpoint hours non-positive, duplicated, or not strictly increasing;
- zero/negative interval;
- activity interval greater than normal interval;
- S/A/B view or comment thresholds not monotonically descending;
- turnover ratio outside `[0, 1]` or S below A;
- `dormant_after_days >= archive_after_days`;
- zero downgrade confirmations;
- malformed `HH:MM` activity window.

- [ ] **Step 2: Run config tests to verify RED**

```powershell
uv run pytest tests/test_cohort_policy_config.py -q
```

Expected: `books_of_time.domain.cohort_policy` does not exist.

- [ ] **Step 3: Implement immutable configuration objects**

`CohortPolicy.from_config` reads the `snapshot_cohorts` mapping only. It accepts partial overrides and returns immutable normalized objects. Use `ZoneInfo`, exact enum keys, integer minute/hour/day values, and `datetime.time` activity boundaries. Keep the initial windows:

```text
lunch  11:30-13:30
dinner 17:30-20:30
night  21:30-00:30
```

The object also carries `enabled` (default `False` during C2), `planning_seconds=30`, `checkpoint_max_lateness=60 minutes`, tier thresholds, official S age, turnover confirmations, reassessment interval, lifecycle intervals, and tier ceilings. Reject booleans where integer values are expected.

- [ ] **Step 4: Update example configuration without activating planner**

Add the approved `snapshot_cohorts` keys to `config/config.yaml.example`, but set:

```yaml
snapshot_cohorts:
  enabled: false  # C2 stores/validates policy; C3 introduces shadow planner
```

Include the 6/12/18/24 checkpoints, tier thresholds, lifecycle, default activity windows, and tier intervals. Do not add C4-C8 execution knobs in this commit.

- [ ] **Step 5: Verify config GREEN**

```powershell
uv run pytest tests/test_cohort_policy_config.py tests/test_config_loader.py -q
uv run ruff check books_of_time/domain/cohort_policy.py tests/test_cohort_policy_config.py
```

Expected: approved defaults parse and every invalid boundary is rejected.

- [ ] **Step 6: Commit**

```powershell
git add books_of_time/domain/cohort_policy.py tests/test_cohort_policy_config.py config/config.yaml.example docs/superpowers/plans/2026-07-14-cohort-state-and-policy-c2.md
git commit -m "feat(policy): validate cohort policy configuration"
```

---

### Task 3: Implement Deterministic Time And Key Policy

**Files:**
- Modify: `books_of_time/domain/cohort_policy.py`
- Create: `tests/test_cohort_time_policy.py`

**Interfaces:**
- Produces `is_activity_window(now: datetime, policy: CohortPolicy) -> bool`.
- Produces `age_growth_interval(anchor: datetime, now: datetime, recent_view_growth_last_hour: int | None) -> timedelta`.
- Produces `effective_interval(..., tier: CollectionTier, policy: CohortPolicy, next_checkpoint_at: datetime | None) -> timedelta`.
- Produces `next_aligned_slot(anchor: datetime, now: datetime, interval: timedelta) -> datetime`.
- Produces `checkpoint_times(anchor: datetime, policy: CohortPolicy) -> tuple[tuple[int, datetime], ...]`.
- Produces stable key helpers `routine_cohort_key`, `checkpoint_cohort_key`, `recovery_cohort_key`, and `component_key`.

- [ ] **Step 1: Write failing UTC/activity/checkpoint tests**

Cover:

- UTC-aware input is required; naive datetimes raise `ValueError`.
- Asia/Shanghai lunch/dinner/night boundaries are start-inclusive/end-exclusive.
- Night `21:30-00:30` crosses midnight; overlaps return one Boolean, not stacked boosts.
- Existing age/growth intervals remain 1m, 5m, 5/15/30/120m at their exact boundaries.
- S active at age 2h yields 2m; S normal yields 5m because age policy is already shorter than its 10m ceiling.
- A active at age 7h and low growth yields 10m; C normal yields 120m.
- A checkpoint 90 seconds away limits the effective interval to 90 seconds; a checkpoint due now returns zero.
- slots remain anchored to immutable publish time, not discovery/wakeup time.
- checkpoint times are exactly anchor + 6/12/18/24h.
- keys canonicalize aware datetimes to whole-second UTC `Z` and reject microsecond ambiguity by truncating consistently.

- [ ] **Step 2: Run time tests to verify RED**

```powershell
uv run pytest tests/test_cohort_time_policy.py -q
```

Expected: the pure functions are missing.

- [ ] **Step 3: Implement time functions without DB access**

Reuse the existing age/growth thresholds, but do not call the database-backed video policy. `effective_interval` computes:

```python
min(
    age_growth_interval,
    policy.tier_intervals[tier].active_or_normal,
    max(next_checkpoint_at - now, timedelta()),  # when supplied
)
```

Use floor-aligned slots from the immutable anchor. Key formats are exact:

```text
snapshot:{bvid}:{YYYY-MM-DDTHH:MM:SSZ}:routine
snapshot:{bvid}:age:{hours}h
snapshot:{bvid}:recovery:through:{hours}h
{cohort_key}:{component_kind}
```

- [ ] **Step 4: Verify time policy GREEN**

```powershell
uv run pytest tests/test_cohort_time_policy.py tests/test_snapshot_policy.py -q
uv run ruff check books_of_time/domain/cohort_policy.py tests/test_cohort_time_policy.py
```

Expected: old cadence remains compatible and C2 interval/key behavior is deterministic.

- [ ] **Step 5: Commit**

```powershell
git add books_of_time/domain/cohort_policy.py tests/test_cohort_time_policy.py docs/superpowers/plans/2026-07-14-cohort-state-and-policy-c2.md
git commit -m "feat(policy): add deterministic cohort time policy"
```

---

### Task 4: Implement Tier Signals And Downgrade Hysteresis

**Files:**
- Modify: `books_of_time/domain/cohort_policy.py`
- Create: `tests/test_cohort_tier_policy.py`

**Interfaces:**
- Produces frozen `TierSignals` and `TierAssessment`.
- Produces `desired_tier(signals: TierSignals, policy: CohortPolicy) -> CollectionTier`.
- Produces `apply_tier_assessment(current_effective, desired, candidate_downgrade, consecutive_count, policy) -> TierAssessment`.

- [ ] **Step 1: Write failing tier decision tests**

Test all independent S paths:

- monitored official + publish age 5h59m => S;
- same video discovered at age 8h => no initial-age S;
- active event core => S;
- `major_creator` involvement => S;
- operator `pinned_tier=S` => S;
- view OR comment OR sustained turnover threshold can independently select S/A/B;
- turnover below its configured confirmation count or with incomplete input is ignored;
- absent numeric evidence falls through to C, never interpreted as zero-based positive evidence;
- model-derived fields do not exist on `TierSignals`.

Test hysteresis:

```text
C -> S desired: immediate S, candidate/count reset
S -> A first assessment: effective S, candidate A, count 1
S -> A second assessment: effective A, candidate NULL, count 0
candidate changes A -> B: count restarts at 1
desired returns to current tier: candidate/count reset
```

- [ ] **Step 2: Run tier tests to verify RED**

```powershell
uv run pytest tests/test_cohort_tier_policy.py -q
```

Expected: tier signal/assessment APIs are missing.

- [ ] **Step 3: Implement OR-first-match tiering**

Represent tier rank explicitly (`S=0`, `A=1`, `B=2`, `C=3`). Apply forced/pinned rules before numeric thresholds. Numeric comparisons are `>=`; thresholds are checked S, then A, then B. Turnover is eligible only when input is complete and confirmations meet policy. `apply_tier_assessment` never changes `desired`; it only determines effective tier and persisted downgrade candidate/count.

- [ ] **Step 4: Verify tier policy GREEN**

```powershell
uv run pytest tests/test_cohort_tier_policy.py tests/test_cohort_policy_config.py -q
uv run ruff check books_of_time/domain/cohort_policy.py tests/test_cohort_tier_policy.py
```

Expected: broad S OR semantics and two-confirmation downgrade are explicit and deterministic.

- [ ] **Step 5: Commit**

```powershell
git add books_of_time/domain/cohort_policy.py tests/test_cohort_tier_policy.py docs/superpowers/plans/2026-07-14-cohort-state-and-policy-c2.md
git commit -m "feat(policy): add cohort tier assessment"
```

---

### Task 5: Implement Lifecycle, Components, And Cohort Status

**Files:**
- Modify: `books_of_time/domain/cohort_policy.py`
- Create: `tests/test_cohort_lifecycle_policy.py`

**Interfaces:**
- Produces `determine_life_stage(...) -> VideoLifeStage`.
- Produces `component_kinds_for_stage(stage, *, frontier_complete: bool) -> tuple[str, ...]`.
- Produces frozen `ComponentOutcome` and `aggregate_cohort_status(outcomes: Sequence[ComponentOutcome]) -> CohortStatus`.

- [ ] **Step 1: Write failing lifecycle and aggregation tests**

Lifecycle cases:

- age below 7 days => active;
- age 7 days + low growth + no event/pin => dormant;
- age 30 days under same conditions => archived;
- active event, operator pin, or renewed growth immediately reactivates dormant/archived;
- missing low-growth evidence does not archive a video.

Component eligibility:

```text
active   -> video_metrics, hot_core, latest_current_head
dormant  -> video_metrics, hot_core, plus latest_current_head only when frontier complete
archived -> video_metrics
```

Aggregation precedence tests:

- any required corrupted => corrupted;
- any required running/joined active => running unless corrupted;
- all pending => planned;
- no component started and all applicable blocked => blocked;
- no component started and terminal miss exists => missed;
- mixed complete and incomplete terminal => partial;
- all applicable required complete and remaining required not-applicable => complete;
- every required component not-applicable => not_applicable.

- [ ] **Step 2: Run lifecycle tests to verify RED**

```powershell
uv run pytest tests/test_cohort_lifecycle_policy.py -q
```

Expected: lifecycle/component/status APIs are missing.

- [ ] **Step 3: Implement pure lifecycle and aggregation rules**

`determine_life_stage` uses immutable publish age and explicit evidence booleans. Reactivation rules win before age thresholds. Do not treat capacity or a missed cohort as lifecycle evidence.

`ComponentOutcome` carries `status`, `required`, and `started`. Aggregation ignores optional components for completeness, follows spec section 6.3 precedence, and raises `ValueError` for an empty required set instead of inventing a complete cohort.

- [ ] **Step 4: Verify lifecycle GREEN**

```powershell
uv run pytest tests/test_cohort_lifecycle_policy.py tests/test_cohort_tier_policy.py tests/test_cohort_time_policy.py -q
uv run ruff check books_of_time/domain/cohort_policy.py tests/test_cohort_lifecycle_policy.py
```

Expected: lifecycle eligibility and cohort status are deterministic with no scheduler dependency.

- [ ] **Step 5: Commit**

```powershell
git add books_of_time/domain/cohort_policy.py tests/test_cohort_lifecycle_policy.py docs/superpowers/plans/2026-07-14-cohort-state-and-policy-c2.md
git commit -m "feat(policy): add cohort lifecycle and status rules"
```

---

### Task 6: Add Policy Activation And Video Adoption Repositories

**Files:**
- Create: `books_of_time/db/cohort_repositories.py`
- Create: `tests/test_cohort_repositories.py`
- Modify: `docs/CONFIGURATION.md`
- Modify: `docs/COLLECTION.md`
- Modify: `docs/DATA_MODEL.md`
- Modify: `docs/TODO.md`

**Interfaces:**
- Produces `CollectionPolicyVersionRepository.create(...)`, `activate(...)`, and `get_active(...)`.
- Produces `VideoCollectionStateRepository.adopt(...)` and `apply_assessment(...)`.
- Consumes C2 ORM models and `TierAssessment`; produces no task or cohort rows.

- [ ] **Step 1: Write failing repository tests**

Using real in-memory ORM:

1. Create versions `v1` and `v2` for the same global scope.
2. Activate v1, then v2; assert only v2 active and v1 superseded timestamp set.
3. Roll back by activating v1; assert policy JSON/version rows were never edited or duplicated.
4. Create a second game scope and assert it may have its own active version.
5. Adopt a `KnownVideo` and assert `schedule_anchor_at == KnownVideo.pubdate`, default state values, and policy version.
6. Re-adopt after changing the supplied timestamp and assert immutable anchor remains unchanged.
7. Apply a `TierAssessment` and lifecycle change; assert desired/effective/candidate/count update, while anchor and pinned tier remain unchanged.

- [ ] **Step 2: Run repository tests to verify RED**

```powershell
uv run pytest tests/test_cohort_repositories.py -q
```

Expected: repository module does not exist.

- [ ] **Step 3: Implement transaction-local repositories**

`create` inserts immutable policy content and scoped evidence counters. `activate` locks active/target rows where supported, supersedes the old active row, and activates the existing target row; it never edits `policy`, algorithm, training bounds, or evidence counts. Normalize global scope to sentinel `scope_id="global"` and reject other global IDs.

`adopt` loads `KnownVideo`, uses its accepted `pubdate` as immutable schedule anchor, and creates one state row. Existing state returns unchanged except `updated_at` is not advanced merely by a read. `apply_assessment` updates only tier/lifecycle policy outputs, policy version, next due metadata explicitly passed by the caller, and `updated_at`.

C2 repository methods only flush. Their caller owns commit/rollback. No method enqueues `CollectionTask` or creates `SnapshotCohort`; C3 adds planner transactions.

- [ ] **Step 4: Verify repository GREEN**

```powershell
uv run pytest tests/test_cohort_repositories.py tests/test_cohort_models.py -q
uv run ruff check books_of_time/db/cohort_repositories.py tests/test_cohort_repositories.py
```

Expected: version rollback preserves immutable rows and adoption preserves publish anchor.

- [ ] **Step 5: Document C2's active and inactive surfaces**

Document:

- all five C2 tables, constraints, status vocabularies, logical task/coverage links;
- policy defaults, activity windows, tier OR semantics, downgrade hysteresis, lifecycle, checkpoints, and key formats;
- `snapshot_cohorts.enabled=false` does not schedule anything in C2;
- existing video snapshot sweep remains the only routine owner until C3 shadow planner and later ownership migration;
- current repository transactions are caller-owned and planner concurrency arrives in C3.

- [ ] **Step 6: Run isolated migration and full verification**

```powershell
uv run pytest tests/test_schema_migrations.py::test_cohort_state_revision_round_trip -q
uv run pytest
uv run ruff check .
git diff --check
```

Expected: isolated migration cycle, complete suite, Ruff, and whitespace checks pass. Do not downgrade the user's configured PostgreSQL database.

- [ ] **Step 7: Mark only C2 complete in TODO**

Change C2 from `[ ]` to `[x]`, keep C3-C9 unchecked, and update Near-term Sprint to say C2 complete/C3 next. Do not mark the overall P1 mainline complete.

- [ ] **Step 8: Commit**

```powershell
git add books_of_time/db/cohort_repositories.py tests/test_cohort_repositories.py docs/CONFIGURATION.md docs/COLLECTION.md docs/DATA_MODEL.md docs/TODO.md docs/superpowers/plans/2026-07-14-cohort-state-and-policy-c2.md
git commit -m "feat(policy): persist cohort policy and video state"
```

## Plan Self-Review Result

- **Spec coverage:** C2 implements sections 6.1-6.5, 7.1-7.6 pure contracts, tier/lifecycle defaults, deterministic status aggregation, and policy/state persistence. Executable planner transactions/idempotent task creation are C3; deep scans are C4; scan-run/CAS state is C5; visibility is C6; capacity and durable short transactions are C7; learned activity versions are C8; live integrity acceptance is C9.
- **Placeholder scan:** No TBD, generic error-handling instruction, or unnamed API remains. Every task has explicit files, interfaces, RED/GREEN commands, and commit boundary.
- **Type consistency:** ORM names, enum values, repository methods, and pure function signatures are defined once and reused by later tasks.
- **Execution choice:** The user already authorized autonomous inline execution and requested restrained subagent use. Execute in the main thread with TDD and per-task commits; no additional approval checkpoint is required.
