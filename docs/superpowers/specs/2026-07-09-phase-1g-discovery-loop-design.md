# Phase 1G Discovery Loop Design

## Context

The project can already perform a one-off UID video-list request via
`discover-user`, parse Bilibili user video-list responses, record fresh
`KnownVideo` rows, and enqueue `fetch_video_stats` tasks. The missing P0 piece
is unattended discovery: scanning configured matrix UIDs on an interval.

Phase 1G adds that loop without introducing event archives, game pools, or
distributed schedulers.

## Goal

Provide a testable discovery loop that periodically scans configured matrix
UIDs and reuses the existing `DiscoveryScheduler`.

The system must support:

1. Config key `discovery.matrix_uids`.
2. Config key `scheduler.discovery_scan_seconds`.
3. A reusable `DiscoveryLoop.run_once()` that scans all configured UIDs once.
4. A reusable `DiscoveryLoop.run_loop()` for unattended operation.
5. CLI command `bot discovery loop`.

## Approved Design Constraints

- Only scan configured matrix UIDs in this slice.
- Event-level UID pools and game-level UID pools remain out of scope.
- Scan page 1 only, ordered by publish time, matching the existing
  `BilibiliPlatformClient.get_user_video_list(mid, page=1)`.
- Do not add Redis, background service management, or a new scheduler daemon.
- Keep the loop testable without real sleep or network.
- Do not bypass platform rate limits; all requests continue through
  `BilibiliPlatformClient`.
- One UID failure should not prevent other UIDs in the same round from being
  scanned.
- Existing `DiscoveryScheduler.handle_discovered_videos()` remains responsible
  for freshness filtering, known-video dedupe, and stats-task enqueue.

## Architecture

Add `books_of_time/task_orchestrator/discovery_loop.py`.

Key types:

```python
@dataclass(frozen=True)
class DiscoveryLoopResult:
    uids_scanned: int
    videos_seen: int
    videos_created: int
    errors: int
```

```python
class DiscoveryVideoClient(Protocol):
    async def get_user_video_list(self, mid: str, page: int = 1) -> FetchResult: ...
```

```python
class DiscoveryLoop:
    async def run_once(self, *, now: datetime | None = None) -> DiscoveryLoopResult: ...

    async def run_loop(
        self,
        *,
        interval_seconds: float,
        max_iterations: int | None = None,
        stop_when_idle: bool = False,
        sleep: Callable[[float], Awaitable[None] | None] | None = None,
    ) -> DiscoveryLoopResult: ...
```

Constructor dependencies:

- `session_factory`
- `client`
- `matrix_uids`
- `fresh_video_window`

`run_once()` flow:

1. Resolve `effective_now`.
2. For each configured UID:
   - request user video list page 1
   - parse via `parse_user_video_list`
   - call `DiscoveryScheduler.handle_discovered_videos(...)`
   - commit the session
3. Return aggregate counts.
4. If one UID raises, roll back that UID session, increment `errors`, log, and
   continue to the next UID.

## CLI

Add:

```text
bot discovery loop --max-iterations 1 --stop-when-idle
```

Options:

- `--interval-seconds`: override `scheduler.discovery_scan_seconds`; default
  from config, falling back to `60`.
- `--max-iterations`: optional finite smoke/test run.
- `--stop-when-idle`: stop after a round creates no videos.

The existing `bot discover-user MID --page N` remains as a manual one-off
debug command.

## Configuration

Use existing example shape:

```yaml
discovery:
  matrix_uids: []

scheduler:
  discovery_scan_seconds: 60
```

`matrix_uids` accepts strings or numbers; runtime normalizes to strings.

## Testing

Required tests:

1. `DiscoveryLoop.run_once()` scans configured UIDs, parses returned videos, and
   enqueues fresh stats tasks.
2. `DiscoveryLoop.run_once()` continues when one UID fails and reports errors.
3. `DiscoveryLoop.run_loop()` supports finite iterations and injectable sleep.
4. CLI parser accepts `bot discovery loop`.
5. CLI helper builds a loop from config and runs a finite smoke iteration in
   tests without real network.

## Acceptance Criteria

- `DiscoveryLoop` exists and is covered by tests.
- `bot discovery loop` exists.
- `discovery.matrix_uids` drives the loop.
- TODO item `实现常驻 discovery loop，每分钟扫描配置的矩阵 UID。` is marked
  complete.
- `uv run pytest` and `uv run ruff check .` pass.

## Out Of Scope

- Event-level UID pools.
- Game-level UID pools.
- Redis Set or external dedupe.
- Late-discovery compensation for videos older than the freshness window.
- Forced 22:00 terminal snapshot tasks.
- Multi-page UID scans.
