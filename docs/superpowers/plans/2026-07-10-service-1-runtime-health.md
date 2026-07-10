# Service-1 Runtime And Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first production long-running entrypoint with deployment environment overrides, durable service heartbeats, startup/health/status checks, and cooperative shutdown around the existing worker.

**Architecture:** Keep one application process and one HTTP worker so the current in-process request limiter remains authoritative. Store service lifecycle state in PostgreSQL, inject a stop event into the worker loop, and expose the same `ServiceHost` through Docker/Linux production and Windows development commands.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy async ORM, PostgreSQL/SQLite tests, argparse, pytest-asyncio, uv, Ruff.

## Global Constraints

- Execute inline in the current session; do not dispatch subagents.
- Docker owns only the application process and connects to an existing PostgreSQL instance.
- Raw and media files remain on local filesystems.
- Worker concurrency remains one until cross-process request budgeting exists.
- Public Bilibili user fields remain stored for manual verification.
- Follow red-green-refactor for every production behavior.
- Keep existing worker and discovery CLI commands as diagnostic compatibility entrypoints.

---

## File Structure

- Modify `books_of_time/config/loader.py`: deployment environment overrides.
- Modify `config/config.yaml.example`: documented service defaults.
- Modify `books_of_time/db/models.py`: `ServiceInstance` lifecycle table.
- Modify `books_of_time/db/repositories.py`: service instance and operational status queries.
- Create `books_of_time/service/__init__.py`: public service exports.
- Create `books_of_time/service/models.py`: health and status value objects.
- Create `books_of_time/service/health.py`: doctor, health, and status checks.
- Create `books_of_time/service/host.py`: worker/heartbeat supervision and cooperative shutdown.
- Modify `books_of_time/worker.py`: stop-event-aware worker loop.
- Modify `books_of_time/app.py`: shared engine/session/worker construction.
- Modify `books_of_time/cli.py`: `service run|doctor|health|status` commands.
- Create `tests/test_service_instances.py`: lifecycle repository tests.
- Create `tests/test_service_health.py`: doctor, heartbeat, storage, and status tests.
- Create `tests/test_service_host.py`: finite run, stop, and failure lifecycle tests.
- Modify `tests/test_config_loader.py`: environment override tests.
- Modify `tests/test_worker_loop.py`: cooperative stop test.
- Modify `tests/test_cli.py`: service command parsing and dispatch tests.
- Modify `docs/TODO.md`: mark only verified Service-1 items complete.

### Task 1: Deployment Configuration Overrides

**Files:**

- Modify: `books_of_time/config/loader.py`
- Modify: `config/config.yaml.example`
- Modify: `tests/test_config_loader.py`

**Interfaces:**

- Produces: `load_config(path: str | Path | None = None, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]`.
- Produces YAML keys `service.instance_id`, `service.roles`, `service.worker_idle_sleep_seconds`, `service.heartbeat_seconds`, `service.heartbeat_timeout_seconds`, and `service.shutdown_grace_seconds`.

- [ ] **Step 1: Write the failing environment override tests**

Add tests that write a minimal YAML file, pass an explicit `environ` mapping,
and assert these exact conversions:

```python
cfg = load_config(
    config_path,
    environ={
        "BOT_DATABASE_URL": "postgresql+asyncpg://host/books",
        "BOT_RAW_DIR": "/archive/raw",
        "BOT_MEDIA_DIR": "/archive/media",
        "BOT_INSTANCE_ID": "collector-a",
        "BOT_SERVICE_ROLES": "worker,scheduler",
        "BOT_SHUTDOWN_GRACE_SECONDS": "45.5",
    },
)

assert cfg["database"]["url"] == "postgresql+asyncpg://host/books"
assert cfg["storage"]["raw_dir"] == "/archive/raw"
assert cfg["storage"]["media_dir"] == "/archive/media"
assert cfg["service"]["instance_id"] == "collector-a"
assert cfg["service"]["roles"] == ["worker", "scheduler"]
assert cfg["service"]["shutdown_grace_seconds"] == 45.5
```

Add a second test proving `BOT_CONFIG` selects the config file only when the
explicit `path` argument is absent.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_config_loader.py -v`

Expected: FAIL because `load_config` does not accept `environ` and ignores
`BOT_CONFIG` and deployment overrides.

- [ ] **Step 3: Implement typed overrides**

Use `os.environ` only when `environ` is omitted. Copy parsed YAML mappings before
mutation, create missing `database`, `storage`, and `service` sections, split
roles on commas while discarding empty values, and parse shutdown grace as
`float`. An explicit function `path` takes precedence over `BOT_CONFIG`.

Append this YAML block to the example configuration:

```yaml
service:
  roles: [worker]
  worker_idle_sleep_seconds: 5
  heartbeat_seconds: 10
  heartbeat_timeout_seconds: 30
  shutdown_grace_seconds: 60
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `uv run pytest tests/test_config_loader.py -v`

Expected: all configuration loader tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add books_of_time/config/loader.py config/config.yaml.example tests/test_config_loader.py
git commit -m "feat: add service environment overrides"
```

### Task 2: Durable Service Instance Lifecycle

**Files:**

- Modify: `books_of_time/db/models.py`
- Modify: `books_of_time/db/repositories.py`
- Create: `tests/test_service_instances.py`

**Interfaces:**

- Produces ORM `ServiceInstance` with primary key `instance_id: str` and fields
  `hostname`, `pid`, `version`, `roles`, `status`, `started_at`, `heartbeat_at`,
  `stopped_at`, `last_error_type`, and `last_error_message`.
- Produces `ServiceInstanceRepository.register(...)`, `mark_running(...)`,
  `heartbeat(...)`, `mark_stopping(...)`, `mark_stopped(...)`, `mark_failed(...)`,
  `get(...)`, `list_recent(...)`, and `has_fresh_running_instance(...)`.

- [ ] **Step 1: Write failing lifecycle repository tests**

Create an in-memory SQLite schema and verify this sequence:

```python
instance = await repo.register(
    instance_id="service-1",
    hostname="collector-host",
    pid=123,
    version="0.1.0",
    roles=["worker"],
    now=started_at,
)
assert instance.status == "starting"

await repo.mark_running("service-1", now=running_at)
await repo.heartbeat("service-1", now=heartbeat_at)
assert await repo.has_fresh_running_instance(
    now=heartbeat_at + timedelta(seconds=20),
    timeout_seconds=30,
)

await repo.mark_stopping("service-1", now=stopping_at)
await repo.mark_stopped("service-1", now=stopped_at)
assert (await repo.get("service-1")).stopped_at == stopped_at
```

Add separate assertions that a stale heartbeat is unhealthy and that
`mark_failed` stores a bounded error type and message.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_service_instances.py -v`

Expected: collection fails because `ServiceInstance` and its repository do not
exist.

- [ ] **Step 3: Implement the model and repository**

Store roles as JSON list with an empty-list default. Use `UTCDateTime` for all
lifecycle timestamps and indexes on `(status, heartbeat_at)` and
`started_at DESC`. Repository status transitions must update `heartbeat_at` and
raise `LookupError` for an unknown instance rather than silently succeeding.
Clamp diagnostic strings to the database column limits before assignment.

- [ ] **Step 4: Run focused and model-dependent tests**

Run: `uv run pytest tests/test_service_instances.py tests/test_task_queue.py -v`

Expected: all tests pass and `Base.metadata.create_all` creates the new table on
SQLite.

- [ ] **Step 5: Commit Task 2**

```bash
git add books_of_time/db/models.py books_of_time/db/repositories.py tests/test_service_instances.py
git commit -m "feat: persist service instance heartbeats"
```

### Task 3: Doctor, Health, And Operational Status

**Files:**

- Create: `books_of_time/service/__init__.py`
- Create: `books_of_time/service/models.py`
- Create: `books_of_time/service/health.py`
- Create: `tests/test_service_health.py`

**Interfaces:**

- Produces immutable `ServiceCheck(name: str, ok: bool, detail: str)`.
- Produces immutable `ServiceHealthReport(checks: tuple[ServiceCheck, ...])`
  with property `ok: bool`.
- Produces immutable `ServiceStatusSnapshot(instances, pending_tasks,
  running_tasks, failed_tasks, oldest_pending_at, active_backoffs)`.
- Produces `ServiceHealthChecker.doctor()`, `health(now: datetime)`, and
  `status(now: datetime, instance_limit: int = 20)`.

- [ ] **Step 1: Write failing health tests**

Cover four behaviors with temporary directories and SQLite:

```python
report = await checker.doctor()
assert report.ok is True
assert {check.name for check in report.checks} == {
    "database",
    "raw_storage",
    "media_storage",
}

health = await checker.health(now=now)
assert health.ok is False
assert next(c for c in health.checks if c.name == "service_heartbeat").ok is False
```

Register a fresh running instance and verify health becomes true. Create pending,
running, and failed tasks plus an active `RequestBackoffState`, then verify the
status snapshot counts and oldest pending timestamp. Point one storage path at
a regular file and verify doctor reports that storage check as failed without
raising.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_service_health.py -v`

Expected: collection fails because the service health modules do not exist.

- [ ] **Step 3: Implement health checks**

Database doctor executes `SELECT 1`. Storage doctor creates each directory and
uses a uniquely named probe file that is removed in `finally`. Health appends a
fresh-running-instance check using `ServiceInstanceRepository`. Status uses
SQLAlchemy aggregate queries and returns data; it does not log or print.
Exceptions become failed checks with exception type and a sanitized message.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `uv run pytest tests/test_service_health.py tests/test_service_instances.py -v`

Expected: all health and lifecycle tests pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add books_of_time/service books_of_time/db/repositories.py tests/test_service_health.py
git commit -m "feat: add service health and status checks"
```

### Task 4: Cooperative Worker Stop And ServiceHost

**Files:**

- Modify: `books_of_time/worker.py`
- Create: `books_of_time/service/host.py`
- Modify: `tests/test_worker_loop.py`
- Create: `tests/test_service_host.py`

**Interfaces:**

- Widens `Worker.run_loop(..., stop_event: asyncio.Event | None = None) -> int`.
- Produces `ServiceHost.request_stop() -> None`.
- Produces `ServiceHost.run(*, max_worker_iterations: int | None = None) -> int`.

- [ ] **Step 1: Write a failing worker cooperative-stop test**

Create an already-set `asyncio.Event`, call `run_loop(stop_event=event)`, and
assert no lease attempt occurs and the result is zero. Add a second case where
an injected sleep sets the event, proving the loop exits after becoming idle.

- [ ] **Step 2: Run the worker loop tests and verify RED**

Run: `uv run pytest tests/test_worker_loop.py -v`

Expected: FAIL because `run_loop` does not accept `stop_event`.

- [ ] **Step 3: Implement minimal stop-event support**

Check the event before each lease attempt and after idle sleep. Preserve all
existing `max_iterations` and `stop_when_idle` behavior.

- [ ] **Step 4: Verify the worker loop is GREEN**

Run: `uv run pytest tests/test_worker_loop.py -v`

Expected: all existing and new worker loop tests pass.

- [ ] **Step 5: Write failing ServiceHost tests**

Use the real in-memory `ServiceInstanceRepository` and a fake worker exposing
the widened `run_loop` signature. Verify:

- finite worker completion transitions `starting -> running -> stopping -> stopped`;
- `request_stop` reaches the worker stop event;
- worker exceptions mark the instance `failed` and are re-raised;
- a worker exceeding `shutdown_grace_seconds` is cancelled after stop is requested.

- [ ] **Step 6: Run ServiceHost tests and verify RED**

Run: `uv run pytest tests/test_service_host.py -v`

Expected: collection fails because `books_of_time.service.host` does not exist.

- [ ] **Step 7: Implement ServiceHost**

The constructor receives the session factory, worker, instance metadata,
heartbeat interval, shutdown grace, and worker idle sleep. `run` registers and
marks the instance running, starts worker and heartbeat tasks, exits when the
worker finishes or `request_stop` is called, and applies the grace timeout only
after a stop request. It records `failed` before re-raising component errors and
never swallows `CancelledError`.

- [ ] **Step 8: Run host and worker tests**

Run: `uv run pytest tests/test_service_host.py tests/test_worker_loop.py tests/test_worker_coverage.py -v`

Expected: all tests pass.

- [ ] **Step 9: Commit Task 4**

```bash
git add books_of_time/worker.py books_of_time/service/host.py tests/test_worker_loop.py tests/test_service_host.py
git commit -m "feat: supervise long-running worker service"
```

### Task 5: Shared Runtime And Service CLI

**Files:**

- Modify: `books_of_time/app.py`
- Modify: `books_of_time/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `docs/TODO.md`

**Interfaces:**

- Produces `build_engine(cfg) -> AsyncEngine`.
- Widens `build_session_factory(cfg, *, engine: AsyncEngine | None = None)`.
- Widens `build_worker(..., session_factory=None, client=None)` so the service
  process owns one engine/session factory/client graph.
- Adds parsers and dispatch for `service run`, `service doctor`,
  `service health`, and `service status`.

- [ ] **Step 1: Write failing CLI parser and dispatch tests**

Assert these parses:

```python
run_args = build_parser().parse_args(
    ["service", "run", "--max-worker-iterations", "1"]
)
assert run_args.service_command == "run"
assert run_args.max_worker_iterations == 1

assert build_parser().parse_args(["service", "doctor"]).service_command == "doctor"
assert build_parser().parse_args(["service", "health"]).service_command == "health"
status_args = build_parser().parse_args(["service", "status", "--limit", "5"])
assert status_args.limit == 5
```

Monkeypatch focused async command helpers and verify `_run` dispatches to each
without constructing unrelated clients.

- [ ] **Step 2: Run CLI tests and verify RED**

Run: `uv run pytest tests/test_cli.py -v`

Expected: FAIL because the `service` parser and handlers do not exist.

- [ ] **Step 3: Refactor shared builders and implement CLI handlers**

`service run` creates one engine, session factory, Bilibili client, worker, and
host; installs `SIGINT` and `SIGTERM` callbacks where the event loop supports
them; and disposes the engine in `finally`. Windows falls back to normal
`KeyboardInterrupt` cancellation semantics without import-time platform
branching.

`doctor` exits with status 1 on a failed report. `health` also requires a fresh
heartbeat. `status` logs the immutable status snapshot and always clamps limit
to 1..200. No handler prints credentials or the database URL.

- [ ] **Step 4: Run CLI and service tests**

Run: `uv run pytest tests/test_cli.py tests/test_service_host.py tests/test_service_health.py -v`

Expected: all tests pass.

- [ ] **Step 5: Run a finite local smoke command against test configuration**

Run: `uv run python main.py --help`

Expected: exits zero and lists the `service` command. Run the finite service
command only when the configured PostgreSQL instance is reachable; otherwise
the automated SQLite ServiceHost test is the acceptance evidence and the live
database limitation is recorded.

- [ ] **Step 6: Update verified TODO items**

Mark complete only the Service-1 items for service package, instance table,
startup checks, run/health/status/doctor, graceful stop, YAML/env configuration,
single-worker gate, Windows entrypoint, and automated service smoke coverage.
Leave persistent jobs, taskified discovery, Docker, systemd, and Alembic items
unchecked for Service-2 and Service-3.

- [ ] **Step 7: Run full verification**

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
git diff --check
```

Expected: all tests pass, Ruff reports no errors or formatting changes, and Git
reports no whitespace errors.

- [ ] **Step 8: Commit Task 5**

```bash
git add books_of_time/app.py books_of_time/cli.py tests/test_cli.py docs/TODO.md
git commit -m "feat: add long-running service commands"
```

## Plan Self-review

- Spec coverage: Service-1 runtime ownership, lifecycle persistence,
  cooperative shutdown, health/status/doctor, configuration, Windows entrypoint,
  and test acceptance are covered by Tasks 1-5.
- Deferred by design: persistent scheduling and taskified discovery are
  Service-2; Docker/systemd/Alembic artifacts are Service-3.
- Placeholder scan: all implementation steps name concrete files, interfaces,
  commands, and expected outcomes.
- Type consistency: `ServiceHost`, `ServiceHealthChecker`, widened worker loop,
  and app builder signatures match their consumers in later tasks.
