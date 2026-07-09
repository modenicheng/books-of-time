# Phase 1H Video Stats Query Design

## Context

Video stats collection already works end to end: `monitor-video` enqueues a
stats task, the worker fetches Bilibili data, archives raw payloads, parses
metrics, and writes `video_metric_snapshots`. The missing P0 read-side piece is
a CLI to inspect collected snapshots for a BV.

Phase 1H adds that query path without changing collection, parsing, or schema.

## Goal

Add `bot video stats BVxxxx` so operators can verify collected video metric
snapshots from the database.

The system must support:

1. Listing recent `video_metric_snapshots` rows for one BV.
2. Ordering snapshots newest first.
3. Limiting result count.
4. Logging all metric columns and `raw_payload_id`.

## Approved Design Constraints

- Read-only; no new collection tasks are created.
- Do not change `video_metric_snapshots` schema in this slice.
- Do not infer missing metrics or compute trend deltas in this slice.
- Keep CLI output log-based, matching existing `coverage` and `task list`
  commands.
- Default limit is `20`; clamp to `1..200`.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and
  `books_of_time/http/rate_limiter.py`.

## Repository API

Add to `VideoMetricSnapshotRepository`:

```python
async def list_for_bvid(
    self,
    *,
    bvid: str,
    limit: int = 20,
) -> list[VideoMetricSnapshot]:
    ...
```

Semantics:

- filter by `VideoMetricSnapshot.bvid == bvid`
- order by `captured_at DESC`
- limit to caller-provided value

## CLI

Extend current `video` subcommands:

```text
bot video stats BVxxxx --limit 20
```

Output one log line per snapshot:

```text
2099-01-01T00:00:00+00:00 bvid=BV... view=... like=... coin=...
favorite=... share=... reply=... danmaku=... raw_payload_id=...
```

If no rows exist, log:

```text
No video stats snapshots for BVxxxx
```

## Testing

Required tests:

1. Repository returns snapshots newest first and respects limit.
2. Parser accepts `bot video stats BVxxxx --limit N`.
3. CLI helper logs stored metrics.
4. CLI helper logs a clear empty-state message.

## Acceptance Criteria

- `VideoMetricSnapshotRepository.list_for_bvid()` exists and is covered by
  tests.
- `bot video stats BVxxxx` exists and is covered by tests.
- TODO item `增加 bot video stats BVxxxx 查询 CLI。` is marked complete.
- `uv run pytest` and `uv run ruff check .` pass.

## Out Of Scope

- Trend/delta calculations.
- Dynamic next snapshot scheduling.
- Video title/description/tag/UP snapshot storage.
- Deletion or visibility state tracking.
- Raw payload reparse from this command.
