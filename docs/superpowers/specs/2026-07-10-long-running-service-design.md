# Long-running Service Design

## Goal

Books of Time must run continuously as a recoverable collection service instead
of relying on an operator to keep independent CLI loops alive. The first
production deployment target is Docker, using an existing PostgreSQL instance.
The same service kernel must also run natively under Linux systemd and directly
on Windows for development.

## Decisions

- Docker packages only the Books of Time application. It does not start or own
  PostgreSQL.
- Raw payloads and media files remain on local mounted filesystems. Media does
  not use S3, OSS, or MinIO.
- PostgreSQL remains the durable task queue and coordination store. Redis is not
  introduced.
- The first production topology uses one application process with cooperative
  scheduler, worker, and heartbeat loops.
- HTTP collectors share one `RawHttpClient` and one in-process rate limiter so
  the configured request budget remains global within the service.
- Existing operational CLI commands remain available for diagnosis and manual
  task management, but they are not the production process supervisor.
- Public Bilibili user fields remain available for manual verification. The
  system limits profiling and analytical claims rather than anonymizing source
  observations.

## Why One Process First

Running scheduler and collector roles in separate containers would give better
fault isolation, but the current token buckets are process-local. Multiple HTTP
processes would each believe they own the full request budget. The initial
service therefore keeps all network collection in one process.

The boundaries still support a later split. Scheduler components only enqueue
durable tasks, workers only lease tasks, and shared state is stored in
PostgreSQL. Multi-process deployment becomes safe after a database-backed or
otherwise distributed request budget is implemented.

## Runtime Architecture

```text
Docker container / Linux systemd / Windows terminal
                         |
                    ServiceHost
          +--------------+--------------+
          |              |              |
     Coordinator      WorkerLoop    HeartbeatLoop
          |              |              |
          +------ PostgreSQL -----------+
                         |
                  collection_tasks
                         |
                  unified HTTP layer
                         |
                 Bilibili public APIs

Filesystem mounts:
- raw payloads: data/raw
- media assets: data/media
```

`ServiceHost` owns process lifetime and constructs one shared runtime:

- SQLAlchemy engine and session factory.
- Bilibili platform client, raw HTTP client, and rate limiter.
- Raw payload and media filesystem stores.
- Collector registry and worker.
- Coordinator, scheduler jobs, and service heartbeat.

The host starts components as supervised asyncio tasks. A component failure is
recorded and causes the process to exit non-zero so Docker or systemd can
restart it. Individual collection failures remain task-level failures and do
not terminate the service.

## Service Lifecycle

Startup order:

1. Load YAML configuration and environment overrides.
2. Validate database connectivity, schema compatibility, and writable storage
   directories.
3. Register a `service_instances` row.
4. Recover expired service and collection leases.
5. Start coordinator, worker, and heartbeat loops.

Shutdown order after `SIGINT` or `SIGTERM`:

1. Mark the instance as stopping and stop scheduling new work.
2. Stop leasing new collection tasks.
3. Allow the active task to finish within the configured grace period.
4. Mark the instance stopped and close HTTP and database resources.
5. If the grace period expires, leave durable leases to be recovered after
   their expiry.

Windows development uses the same cooperative stop path when Ctrl+C is pressed.

## Persistent Service State

### `service_instances`

One row represents one running service process:

- `instance_id`: stable ID supplied by configuration or generated at startup.
- `hostname`, `pid`, and `version`: diagnostic identity.
- `roles`: enabled runtime roles.
- `status`: `starting`, `running`, `stopping`, `stopped`, or `failed`.
- `started_at`, `heartbeat_at`, and `stopped_at`.
- `last_error_type` and `last_error_message`.

A heartbeat older than the configured threshold is unhealthy. Historical rows
are retained for operational audit.

### `scheduled_jobs`

One row represents one durable periodic job:

- `job_key`: globally unique stable key.
- `job_kind`: discovery scan, snapshot sweep, terminal snapshot, maintenance,
  or later event scheduling.
- `schedule_seconds` and `next_run_at`.
- `lease_owner` and `lease_until`.
- `last_started_at`, `last_succeeded_at`, and `last_failed_at`.
- `consecutive_failures`, `last_error_type`, and `last_error_message`.
- `enabled` and a JSON payload.

The coordinator atomically leases due jobs. Success advances `next_run_at`
from the intended schedule, with bounded catch-up, so a restart does not cause
an uncontrolled burst. Failure records the error and schedules a bounded retry.

## Scheduler Responsibilities

Periodic scheduling is separated from collection:

- UID discovery scans enqueue `FETCH_USER_VIDEOS` tasks.
- Video snapshot sweeps enqueue due `FETCH_VIDEO_STATS` tasks.
- Daily terminal snapshot scheduling is an independent job and runs even when
  no discovery UID is configured.
- Event target discovery plugs into the same coordinator after Event Archive is
  implemented.
- Media similarity and maintenance jobs remain low priority and cannot displace
  comment collection budgets.

The current direct-request `DiscoveryLoop` remains temporarily available for
diagnosis, then becomes a thin compatibility command around task enqueueing.

## Taskified Discovery

`FETCH_USER_VIDEOS` is handled by a normal collector:

1. Coordinator enqueues one idempotent task per UID and schedule window.
2. Worker leases the task.
3. Collector requests the public submission list through the shared Bilibili
   client and rate limiter.
4. The response is archived as raw evidence and receives coverage metadata.
5. Parsed videos upsert `known_videos` and preserve source pool information.
6. Newly discovered videos enqueue initial video stats and comment tasks.

This brings discovery under the same raw evidence, retry, backoff, coverage, and
request-budget rules as every other collector.

## Health And Operations

The initial release does not add an HTTP server. Operational commands are:

- `bot service run`: production long-running entrypoint.
- `bot service health`: exits non-zero when database, storage, schema, or
  heartbeat checks fail; used by Docker `HEALTHCHECK`.
- `bot service status`: prints service instances, queue depth, oldest pending
  task age, failed task count, and active request backoffs.
- `bot service doctor`: performs startup validation without starting loops.

Logs go to stdout/stderr for Docker and systemd collection. Secrets are never
included in health output or logs.

## Configuration

YAML remains the base configuration. Environment variables override deployment
specific values:

- `BOT_CONFIG`
- `BOT_DATABASE_URL`
- `BOT_RAW_DIR`
- `BOT_MEDIA_DIR`
- `BOT_INSTANCE_ID`
- `BOT_SERVICE_ROLES`
- `BOT_SHUTDOWN_GRACE_SECONDS`

Service configuration includes heartbeat interval and timeout, worker idle
sleep, scheduler poll interval, shutdown grace, and enabled roles. Initial
worker concurrency is one.

## Deployment

### Docker

- Build one Python 3.12 application image with `uv.lock` in frozen mode.
- Run as a non-root user.
- Mount raw and media directories as persistent host paths or named volumes.
- Supply the external database URL through an environment file or secret.
- Do not include a PostgreSQL service in Compose.
- On Linux, Compose may map `host.docker.internal` to `host-gateway` when the
  database runs on the Docker host.

The host PostgreSQL instance must listen on a reachable interface and its
`pg_hba.conf` must permit the Docker bridge or application host. These database
network changes are deployment prerequisites, not actions performed by the
application.

### Linux Native

A systemd unit runs the same `service run` command from a project virtual
environment, loads an environment file, restarts on failure, and uses fixed
raw/media state directories. Database migrations are an explicit deployment
step and are not automatically applied on every process start.

### Windows Development

Developers run `uv run python main.py service run`. Finite-loop and short
interval options are available only for tests and smoke runs. Existing CLI
commands remain usable for inspecting and manipulating tasks.

## Database Migration Policy

Long-running deployments require reproducible schema history. Alembic revision
files must become committed project artifacts. Deployment runs `alembic upgrade
head` explicitly before starting a new application version. `service doctor`
must reject an incompatible or unreachable schema; `service run` does not
silently create or migrate tables.

## Delivery Phases

### Service-1: Runtime And Health

- Shared runtime ownership.
- `service_instances` persistence and heartbeat.
- `ServiceHost` with graceful shutdown.
- `service run`, `health`, `status`, and `doctor` commands.

Acceptance: a finite smoke run starts all enabled loops, updates heartbeat,
executes queued work, and stops cleanly without losing durable tasks.

### Service-2: Persistent Scheduling And Discovery

- `scheduled_jobs` leases and retries.
- Taskified UID discovery with raw archive and coverage.
- Independent video snapshot and terminal snapshot jobs.

Acceptance: restarting the service resumes due scheduling without duplicate
active tasks and discovery requests use the unified worker request path.

### Service-3: Deployment Artifacts

- Dockerfile, application-only Compose example, and health check.
- Linux systemd example and deployment guide.
- Windows development documentation.
- Committed Alembic migration policy and initial revisions.

Acceptance: Docker and Linux native processes connect to an existing PostgreSQL
instance and use persistent local raw/media directories.

### Service-4: Scaling Gate

- Distributed request-budget design.
- Role-specific processes and worker replication only after that budget exists.

Acceptance: multiple collector processes cannot exceed configured platform or
request-type limits.

## Implementation Order

Service-1 and Service-2 are implemented before Event Archive because event
target discovery depends on a reliable coordinator. Service-3 follows once the
runtime contract is stable. The remaining Important Replies priority factors
can proceed independently, while folded/unfolded visibility events remain
blocked on source fields from the platform parser.

## Testing

- Repository tests cover heartbeat transitions, stale detection, job leasing,
  lease recovery, and schedule advancement.
- Service tests use finite loops and injected clocks/sleep functions.
- Signal behavior is tested through the host stop event instead of sending real
  operating-system signals in unit tests.
- Collector tests verify discovery raw archive, coverage, idempotency, and
  failure backoff.
- Docker image construction and CLI doctor/health commands are smoke-tested.
- The complete pytest and Ruff suites remain mandatory before each feature
  commit.

## Non-goals

- Bundling or managing PostgreSQL.
- Introducing Redis, Kubernetes, or a web dashboard.
- Running multiple HTTP worker processes before distributed rate limiting.
- Automatically changing host PostgreSQL network configuration.
- Moving media files to object storage.
- Automatically applying schema migrations during normal service startup.
