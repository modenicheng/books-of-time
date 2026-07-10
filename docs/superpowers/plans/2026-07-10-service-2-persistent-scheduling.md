# Service-2 Persistent Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist periodic scheduler state, move UID discovery requests into the worker evidence pipeline, and run independent video snapshot and terminal snapshot scheduling from the long-running service.

**Architecture:** PostgreSQL stores one leased `scheduled_jobs` row per periodic responsibility. A coordinator executes database-only job handlers; UID handlers enqueue `DISCOVER_USER_VIDEOS`, whose collector performs the actual Bilibili request through the existing shared HTTP client, archives raw evidence, records a raw page observation, and produces coverage.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy async ORM, PostgreSQL/SQLite tests, existing task queue and collectors, pytest-asyncio, uv, Ruff.

## Global Constraints

- Execute inline; do not dispatch subagents.
- Keep one HTTP worker process and the existing in-process global limiter.
- Scheduled job handlers enqueue durable tasks or inspect local database state; they do not call Bilibili directly.
- Use existing `TaskKind.DISCOVER_USER_VIDEOS` and `BilibiliRequestType.USER_VIDEO_LIST` values.
- Discovery requests must save raw payload, raw page observation, and coverage.
- Terminal snapshot scheduling must run even with an empty UID source list.
- Keep the direct `discovery loop` CLI temporarily as a compatibility diagnostic.
- Follow red-green-refactor and make one commit per task.

---

## File Structure

- Modify `books_of_time/db/models.py`: `ScheduledJob` table.
- Modify `books_of_time/db/repositories.py`: job registration, leasing, success, failure, and recovery.
- Modify `books_of_time/domain/enums.py`: stable scheduled job kinds.
- Create `books_of_time/service/coordinator.py`: coordinator loop and handler protocol.
- Create `books_of_time/service/scheduled_jobs.py`: default job definitions and handlers.
- Create `books_of_time/collectors/user_videos.py`: taskified UID discovery collector.
- Modify `books_of_time/parsers/discovery.py`: parser version constant.
- Modify `books_of_time/app.py`: collector and coordinator construction.
- Modify `books_of_time/service/host.py`: supervise optional coordinator loop.
- Modify `books_of_time/cli.py`: reuse shared UID source resolver and wire coordinator.
- Modify `books_of_time/task_orchestrator/video_snapshot_scheduler.py`: due snapshot sweep.
- Create `books_of_time/task_orchestrator/discovery_sources.py`: config-to-source mapping.
- Create `tests/test_scheduled_jobs.py`: repository lifecycle and recovery.
- Create `tests/test_service_coordinator.py`: bootstrap, handler success/failure, and stop behavior.
- Create `tests/test_user_videos_worker.py`: raw, page, coverage draft, and new-video task behavior.
- Create `tests/test_service_scheduled_handlers.py`: UID, due snapshot, and terminal handlers.
- Modify `tests/test_service_host.py`: coordinator supervision.
- Modify `tests/test_cli.py`: source resolver compatibility and finite service smoke.
- Modify `docs/TODO.md`: mark only verified Service-2 work complete.

### Task 1: Scheduled Job Persistence

**Interfaces:**

- `ScheduledJobKind`: `UID_DISCOVERY`, `VIDEO_SNAPSHOT_SWEEP`, `DAILY_TERMINAL_SNAPSHOT`.
- `ScheduledJobRepository.ensure(...) -> ScheduledJob`.
- `ScheduledJobRepository.lease_due(lease_owner, now, lease_seconds) -> ScheduledJob | None`.
- `ScheduledJobRepository.mark_succeeded(job, now) -> ScheduledJob`.
- `ScheduledJobRepository.mark_failed(job, now, retry_delay_seconds, error) -> ScheduledJob`.

- [ ] Write `tests/test_scheduled_jobs.py` covering idempotent ensure, priority/due ordering, exclusive lease, expired lease recovery, aligned next-run advancement, and bounded failure diagnostics.
- [ ] Run `uv run pytest tests/test_scheduled_jobs.py -v`; verify collection fails because model/repository are absent.
- [ ] Add `ScheduledJob` with unique `job_key`, enum `job_kind`, positive `schedule_seconds`, priority, JSON payload, enabled flag, next-run/lease timestamps, last outcome timestamps, failure count, and bounded diagnostics.
- [ ] Implement repository methods. Success advances to the first aligned slot strictly after `now`, clears lease/errors, and resets failures. Failure clears lease and schedules `now + retry_delay_seconds`.
- [ ] Run `uv run pytest tests/test_scheduled_jobs.py tests/test_task_queue.py -v`; verify GREEN.
- [ ] Commit with `feat: persist scheduled service jobs`.

### Task 2: Coordinator Loop

**Interfaces:**

- `ScheduledJobDefinition(job_key, job_kind, schedule_seconds, priority, payload)`.
- `ScheduledJobHandler.handle(job, session, now) -> None` protocol.
- `ScheduledJobCoordinator.bootstrap(now) -> None`.
- `ScheduledJobCoordinator.run_once(now=None) -> bool`.
- `ScheduledJobCoordinator.run_loop(stop_event, max_iterations=None) -> int`.

- [ ] Write `tests/test_service_coordinator.py` proving bootstrap idempotency, successful handler execution and advancement, handler failure persistence without coordinator crash, unknown kind failure persistence, and stop-event exit before leasing.
- [ ] Run the focused tests and verify RED because coordinator modules are absent.
- [ ] Implement bootstrap through `ensure`. Lease and commit before handler execution. Execute handler in a new transaction; on error roll it back and mark the leased job failed in a fresh transaction so partial handler writes never commit.
- [ ] Add cooperative idle sleep and stop-event support matching worker semantics.
- [ ] Run `uv run pytest tests/test_service_coordinator.py tests/test_scheduled_jobs.py -v`; verify GREEN.
- [ ] Commit with `feat: coordinate persistent scheduler jobs`.

### Task 3: Taskified UID Discovery Collector

**Interfaces:**

- `DISCOVERY_PARSER_VERSION = "bilibili-user-video-list-v1"`.
- `UserVideosCollector.collect(task, session) -> CoverageDraft`.
- Task payload keys: `mid`, `page`, `source_pool_type`, `source_pool_id`, and `reason`.

- [ ] Write `tests/test_user_videos_worker.py` using a fake Bilibili client and real `Worker`. Assert one raw payload, one `RawPageObservation` for target type `user`, one known video, one stats task, successful coverage, source-pool metadata, and no duplicate entity/task on repeated page content.
- [ ] Add a malformed JSON test and verify worker records parse-error coverage/backoff while retaining the raw payload.
- [ ] Run focused tests and verify RED because the collector is absent and worker does not register this behavior.
- [ ] Implement the collector: request through `get_user_video_list`, archive `.json.zst`, insert raw metadata with parser version, parse via `parse_user_video_list`, insert a discovery raw-page row, and call `DiscoveryScheduler.handle_discovered_videos`.
- [ ] Return coverage with one requested/succeeded page, observed video count, one raw payload, and `videos_created` in `extra`.
- [ ] Register `TaskKind.DISCOVER_USER_VIDEOS` in `build_worker`.
- [ ] Run `uv run pytest tests/test_user_videos_worker.py tests/test_discovery_scheduler.py tests/test_worker_coverage.py -v`; verify GREEN.
- [ ] Commit with `feat: collect user video discovery tasks`.

### Task 4: Default Scheduled Handlers

**Interfaces:**

- `resolve_discovery_uid_sources(discovery_cfg) -> list[DiscoveryUidSource]`.
- `UidDiscoveryScheduleHandler.handle(job, session, now)`.
- `VideoSnapshotSweepScheduleHandler.handle(job, session, now)`.
- `TerminalSnapshotScheduleHandler.handle(job, session, now)`.
- `VideoSnapshotScheduler.schedule_due_snapshots(session, now, limit=500)`.
- `build_default_scheduled_jobs(cfg, session_factory) -> (definitions, handlers)`.

- [ ] Move UID source resolution from CLI to `discovery_sources.py` and preserve all existing matrix/game/event pool tests.
- [ ] Write handler tests proving per-UID idempotent task enqueue keyed by scheduled slot, no UID network call, terminal scheduling with zero UIDs, and due video snapshot scheduling from the latest persisted metric timestamp.
- [ ] Run focused tests and verify RED for missing handlers and sweep API.
- [ ] Implement UID handler using `TaskKind.DISCOVER_USER_VIDEOS`. Implement terminal handler through existing terminal scheduler. Implement sweep by deriving each video's policy due time from its latest metric capture and enqueue only when due; unavailable videos remain excluded.
- [ ] Build three definitions using configured discovery interval, 60-second snapshot sweep, and 60-second terminal check. All begin due at coordinator bootstrap.
- [ ] Run handler, discovery, and snapshot scheduler tests; verify GREEN.
- [ ] Commit with `feat: schedule discovery and snapshot jobs`.

### Task 5: ServiceHost Integration And Acceptance

**Interfaces:**

- Widen `ServiceHost(..., coordinator: ServiceCoordinator | None = None)`.
- `ServiceCoordinator.run_loop(*, stop_event, max_iterations=None) -> int` protocol.
- `build_service_coordinator(cfg, session_factory, instance_id) -> ScheduledJobCoordinator`.

- [ ] Add host tests proving coordinator and worker share the same stop event, coordinator exceptions mark the service failed, and finite worker completion stops the coordinator.
- [ ] Run host tests and verify RED because coordinator supervision is absent.
- [ ] Start the optional coordinator beside worker and heartbeat. Any loop-level coordinator exception fails the service; individual scheduled handler failures remain persisted and do not raise from the coordinator.
- [ ] Wire the production CLI service runtime to the coordinator built from the same session factory. Keep the finite SQLite smoke deterministic.
- [ ] Update TODO items for scheduled jobs, taskified UID discovery, independent snapshot sweep, independent terminal scheduling, and automated restart/idempotency coverage.
- [ ] Run `uv run python main.py --help`, full pytest, Ruff lint, Ruff format check, and `git diff --check`.
- [ ] If local PostgreSQL schema is intentionally upgraded for this branch, run `service doctor`, a finite service iteration, and `service status`; otherwise record SQLite as the current runtime smoke and defer PostgreSQL mutation to Service-3 migrations.
- [ ] Commit with `feat: run persistent service coordinator`.

## Plan Self-review

- All Service-2 acceptance points map to Tasks 1-5.
- Bilibili network access occurs only in the worker collector, never in scheduled handlers.
- UID, snapshot sweep, and terminal scheduling remain independently testable.
- Persistent leases make restarts safe; active task idempotency prevents duplicate work.
- Docker/systemd/Alembic artifacts remain Service-3 and are not mixed into this plan.
