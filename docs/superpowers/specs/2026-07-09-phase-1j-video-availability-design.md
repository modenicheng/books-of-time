# Phase 1J Video Availability Design

## Context

Video stats collection now stores metric snapshots and metadata snapshots from
the same archived `Video.get_info()` payload. The next P0 TODO is to record
when a monitored video is deleted, invisible, or blocked by a permission error.

The current worker already records platform-level request failures such as
HTTP 403, 429, captcha, and 5xx in request backoff state. That is not enough for
video monitoring because operators also need a per-BV history of the target
video's availability.

## Goal

Record a per-video availability snapshot for each video stats payload that is
successfully archived.

The system must persist:

1. BV id, using task target/payload as fallback when the response has no data.
2. Capture timestamp.
3. Availability status.
4. Bilibili business `code` and `message` when present.
5. HTTP status code.
6. `raw_payload_id`.

## Approved Design Constraints

- Keep platform/global failures in `RequestBackoffState`; this slice adds
  target-level availability records.
- Use a separate `video_availability_snapshots` table instead of overloading
  metrics or info snapshots.
- Insert a `visible` availability row for normal payloads.
- For known target-unavailable payloads, insert availability and finish the
  collection task without writing metric/info snapshots.
- Do not add new Bilibili API requests.
- Do not anonymize owner or video identifiers.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and
  `books_of_time/http/rate_limiter.py`.
- Execute inline in this main session; do not dispatch subagents unless the user
  asks again.

## Availability Statuses

Use text statuses:

- `visible`: response code is `0` or absent and `data.bvid` is present.
- `deleted`: business code or message indicates the video does not exist or was
  deleted.
- `invisible`: message indicates hidden, unavailable, review, or not visible.
- `permission_denied`: business code or message indicates permission denial.
- `unknown_error`: response has a non-zero business code that is not classified.

The parser should be conservative. It must not claim a video is deleted unless
the response explicitly indicates deletion or non-existence.

## Schema

Add `video_availability_snapshots`:

```text
bvid TEXT PRIMARY KEY
captured_at UTCDateTime PRIMARY KEY
status TEXT NOT NULL
bili_code BIGINT NULL
bili_message TEXT NULL
http_status_code INTEGER NULL
raw_payload_id BIGINT NULL
```

Indexes:

- `idx_video_availability_snapshots_bvid_time` on `bvid, captured_at DESC`
- `idx_video_availability_snapshots_status_time` on `status, captured_at DESC`

## Parser

Add `ParsedVideoAvailabilitySnapshot` and
`parse_video_availability_snapshot()`.

Signature:

```python
def parse_video_availability_snapshot(
    payload: dict[str, Any],
    *,
    captured_at: datetime,
    raw_payload_id: int | None,
    requested_bvid: str,
    http_status_code: int | None,
) -> ParsedVideoAvailabilitySnapshot:
    ...
```

The parser chooses `bvid` from `data.bvid` first, then `requested_bvid`.

## Collector Flow

`VideoStatsCollector.collect()` should:

1. Fetch and archive raw payload once.
2. Decode JSON once.
3. Parse and insert availability.
4. If availability is not `visible`, return coverage with
   `items_observed=0` and `reason=<status>`.
5. If availability is `visible`, continue inserting metric and info snapshots.

Malformed visible payloads still raise `ParseFailure`; availability rows are
not a substitute for required metric parsing on visible videos.

## Testing

Required tests:

1. Parser returns `visible` for normal payloads.
2. Parser maps deleted, invisible, permission-denied, and unknown business error
   payloads.
3. Repository inserts availability snapshots.
4. Worker writes visible availability beside metric/info snapshots.
5. Worker records deleted availability and completes without metric/info rows.

## Acceptance Criteria

- `video_availability_snapshots` model exists and is created by `Base.metadata`.
- Every successful video stats payload creates an availability snapshot.
- Known unavailable business payloads do not produce parse backoff.
- TODO item `记录视频删除、不可见、权限异常状态。` is marked complete.
- `uv run pytest` and `uv run ruff check .` pass.

## Out Of Scope

- Retrying or suppressing HTTP-level platform failures.
- CLI query for availability snapshots.
- Dynamic next snapshot scheduling.
- Exact, exhaustive Bilibili error-code taxonomy.
