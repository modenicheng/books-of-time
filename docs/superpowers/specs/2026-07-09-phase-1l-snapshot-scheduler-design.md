# Phase 1L Snapshot Scheduler Design

## Context

The project can now compute a dynamic next snapshot time for a BV from stored
metric snapshots. The remaining P0 video-metrics TODO is to use that policy to
schedule follow-up `FETCH_VIDEO_STATS` tasks.

## Goal

After a visible video stats collection succeeds, enqueue the next stats task for
known discovered videos.

## Approved Design Constraints

- Schedule only videos present in `known_videos`; manual one-off `monitor-video`
  tasks without known `pubdate` are not auto-looped in this slice.
- Schedule only visible collections. Deleted, invisible, and permission-denied
  payloads should not enqueue another stats task.
- Use `get_next_video_snapshot_at()` for timing.
- Use an idempotency key containing BV and next timestamp.
- Do not add new Bilibili API requests.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and
  `books_of_time/http/rate_limiter.py`.

## Scheduler API

Add `VideoSnapshotScheduler` in
`books_of_time/task_orchestrator/video_snapshot_scheduler.py`:

```python
async def schedule_next_for_video(
    self,
    *,
    session: AsyncSession,
    bvid: str,
    now: datetime,
) -> CollectionTask | None:
    ...
```

Semantics:

- Load `KnownVideo` by `bvid`.
- Return `None` if unknown.
- Call `get_next_video_snapshot_at(session, bvid=bvid, published_at=known.pubdate, now=now)`.
- Return `None` if policy returns `None` outside the core window.
- Enqueue a `FETCH_VIDEO_STATS` task with `not_before=next_at`, priority `80`,
  payload reason `snapshot_policy`, and idempotency key
  `fetch_video_stats:video:<bvid>:snapshot:<next_at.isoformat()>`.

## Collector Integration

`VideoStatsCollector.collect()` should accept an optional scheduler. If supplied
and the availability status is `visible`, it schedules the next stats task after
metric and info snapshots have been flushed.

The default remains no scheduler to keep existing tests and manual collectors
lightweight unless the app wires it.

## Testing

Required tests:

1. Scheduler enqueues a next stats task for a known video.
2. Scheduler returns `None` for unknown videos.
3. Worker collection schedules the next task after a visible stats snapshot.
4. Worker collection does not schedule a next task for deleted payloads.

## Acceptance Criteria

- Snapshot policy is used to enqueue follow-up tasks.
- Manual unknown videos do not auto-loop.
- TODO item `将快照策略接入 scheduler，而不只是纯函数测试。` is marked complete.
- `uv run pytest` and `uv run ruff check .` pass.

## Out Of Scope

- CLI controls for enabling/disabling policy scheduling.
- Configurable policy thresholds.
- Availability-driven terminal state policies beyond not scheduling another task.
