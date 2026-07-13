# Discovery Schedule Policy Implementation Plan

> **For agentic workers:** Execute inline in the main session. Subagents are
> explicitly disabled for this repository task. Steps use checkbox (`- [ ]`)
> syntax for tracking.

**Goal:** Restrict automatic new-video discovery to the approved daytime window
and focus minutes while allowing snapshots and queued collection work to run
continuously.

**Architecture:** A pure `DiscoverySchedulePolicy` owns timezone-aware window
and focus classification. The persisted UID handler consumes that policy and
adds auditable task metadata. Snapshot interval calculation no longer knows
about the discovery window; the daily terminal checkpoint gets its own schedule
type.

**Tech Stack:** Python 3.12, SQLAlchemy asyncio, pytest, Ruff, YAML.

## Global Constraints

- Use Asia/Shanghai unless configuration explicitly overrides it.
- Treat 10:00 as inclusive and 22:00 as exclusive.
- Do not gate comment, reply, media, retry, or video snapshot work by discovery
  time.
- Preserve Windows, Linux native, and Docker operation.
- Do not use subagents.

---

### Task 1: Pure Discovery Policy

**Files:**
- Create: `books_of_time/task_orchestrator/discovery_schedule_policy.py`
- Create: `tests/test_discovery_schedule_policy.py`

- [x] Write boundary, timezone, focus, and validation tests.
- [x] Run the focused test and observe the missing-policy failure.
- [x] Implement immutable policy parsing and classification.
- [x] Run the focused test to green.

### Task 2: Persisted UID Handler

**Files:**
- Modify: `books_of_time/service/scheduled_jobs.py`
- Modify: `tests/test_service_scheduled_handlers.py`

- [x] Add failing tests for inactive, normal, focus, and configured schedules.
- [x] Inject the policy into the handler and classify `job.next_run_at`.
- [x] Store `discovery_schedule_mode` and `focus_time` in task payloads and use
  higher priority for focus slots.
- [x] Run scheduled-handler tests to green.

### Task 3: Continuous Video Snapshots

**Files:**
- Modify: `books_of_time/task_orchestrator/snapshot_policy.py`
- Modify: `books_of_time/task_orchestrator/video_snapshot_policy.py`
- Modify: `books_of_time/task_orchestrator/video_snapshot_scheduler.py`
- Modify: `tests/test_snapshot_policy.py`
- Modify: `tests/test_video_snapshot_scheduler.py`

- [x] Replace the outside-window expectation with a failing all-day interval
  expectation.
- [x] Remove discovery-window gating from the snapshot policy and sweep.
- [x] Give the 22:00 additive checkpoint a dedicated terminal schedule type.
- [x] Run snapshot tests to green.

### Task 4: Configuration And Operator Contract

**Files:**
- Modify: `config/config.yaml.example`
- Modify: `docs/TODO.md`
- Modify: `docs/CONFIGURATION.md`
- Modify: `docs/COLLECTION.md`
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/USER_GUIDE.md`
- Modify: `docs/TROUBLESHOOTING.md`

- [x] Replace obsolete snapshot-window wording with discovery-window settings.
- [x] State precisely that queued collection runs 24/7 and that the terminal
  snapshot is additive.
- [x] Record the corrected scheduling contract in TODO.

### Task 5: Verification And Commit

- [x] Run focused policy, handler, and snapshot tests.
- [x] Run `uv run pytest`.
- [x] Run `uv run ruff check .` and `uv run ruff format --check .`.
- [x] Run Alembic metadata and both Compose configuration checks.
- [x] Review the diff and commit the scheduling change independently.
