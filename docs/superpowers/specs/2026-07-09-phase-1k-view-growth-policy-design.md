# Phase 1K View Growth Policy Design

## Context

`snapshot_policy.py` already changes the snapshot interval from a
`recent_view_growth_last_hour` input. The missing piece is deriving that input
from stored `video_metric_snapshots`.

## Goal

Compute recent one-hour view growth from stored metric snapshots and expose a
small helper that returns the next snapshot time for one BV.

## Approved Design Constraints

- Do not enqueue scheduler tasks in this slice.
- Reuse `get_next_snapshot_at()`; do not duplicate interval thresholds.
- Use `VideoMetricSnapshot.view_count` only.
- Return `None` for growth when no usable snapshots exist.
- Clamp negative growth to `0`.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and
  `books_of_time/http/rate_limiter.py`.
- Execute inline in this main session; do not dispatch subagents unless the user
  asks again.

## Repository API

Add to `VideoMetricSnapshotRepository`:

```python
async def get_view_growth_since(
    self,
    *,
    bvid: str,
    since: datetime,
    now: datetime,
) -> int | None:
    ...
```

Semantics:

- latest snapshot: newest row where `captured_at <= now` and `view_count` is not
  `None`.
- baseline snapshot: newest row where `captured_at <= since` and `view_count` is
  not `None`; if absent, use the oldest usable row in `(since, now]`.
- return `latest.view_count - baseline.view_count`, clamped to `0`.
- return `None` if latest and baseline cannot both be found, or if they are the
  same row.

## Policy Service

Add `books_of_time/task_orchestrator/video_snapshot_policy.py`:

```python
async def get_next_video_snapshot_at(
    session: AsyncSession,
    *,
    bvid: str,
    published_at: datetime,
    now: datetime,
    core_window: CoreWindow | None = None,
) -> datetime | None:
    ...
```

The helper computes one-hour growth with the repository and passes it to
`get_next_snapshot_at()`.

## Testing

Required tests:

1. Repository computes one-hour growth using a baseline before the cutoff.
2. Repository uses the oldest in-window row when no pre-cutoff baseline exists.
3. Repository clamps negative growth to `0`.
4. Service passes computed growth into the existing policy and returns the
   expected next snapshot time.

## Acceptance Criteria

- Stored metric snapshots can produce a recent one-hour view increment.
- A BV-level helper returns dynamic next snapshot time from database state.
- TODO item `基于最近 1 小时播放增量计算动态下次快照时间。` is marked complete.
- `uv run pytest` and `uv run ruff check .` pass.

## Out Of Scope

- Enqueuing the next snapshot task.
- Updating discovery scheduler behavior.
- Filling missing historical points by interpolation.
