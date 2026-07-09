# Phase 1I Video Info Snapshots Design

## Context

Video metric collection already uses Bilibili `Video.get_info()` and archives
the complete raw payload before parsing metric counters. The same payload also
contains stable operator-facing metadata such as title, description, owner, and
category or tag fields.

The next P0 TODO item is to save these metadata fields as snapshots so an
operator can verify what the monitored video looked like at collection time.

## Goal

Save a video metadata snapshot whenever a video stats task succeeds.

The system must persist:

1. BV id.
2. Capture timestamp.
3. Video title.
4. Video description.
5. UP owner MID and display name.
6. Tag or category names available in the `get_info()` payload.
7. The `raw_payload_id` that produced the snapshot.

## Approved Design Constraints

- Use the existing `get_video_stats()` request; do not add another Bilibili API
  request in this slice.
- Keep snapshots append-only, keyed by `bvid` and `captured_at`, matching
  `video_metric_snapshots`.
- Store user and owner data without anonymization so operators can verify
  system behavior.
- Store tags in a JSON object so the parser can keep both normalized names and
  lightweight source hints without schema churn.
- Missing optional metadata fields should be stored as `None` or empty tag
  names, not treated as parse failure.
- Missing `data.bvid` remains a parse failure because the row cannot be keyed.
- Preserve unrelated dirty changes in `books_of_time/http/client.py`,
  `books_of_time/http/rate_limiter.py`, `books_of_time/cli.py`, and
  `tests/test_cli.py` unless this slice explicitly needs those files.
- Execute inline in this main session; do not dispatch subagents unless the user
  asks again.

## Schema

Add `video_info_snapshots`:

```text
bvid TEXT PRIMARY KEY
captured_at UTCDateTime PRIMARY KEY
title TEXT NULL
description TEXT NULL
owner_mid BIGINT NULL
owner_name TEXT NULL
tags JSON NOT NULL
raw_payload_id BIGINT NULL
```

Indexes:

- `idx_video_info_snapshots_bvid_time` on `bvid, captured_at DESC`

`tags` stores:

```json
{
  "names": ["游戏", "攻略"],
  "source_fields": ["tag", "tname"]
}
```

## Parser

Add `ParsedVideoInfoSnapshot` and `parse_video_info_snapshot()`.

The parser reads from `payload["data"]` and accepts these tag sources:

- `data["tag"]` or `data["tags"]` list entries as strings or dictionaries.
- dictionary names from `tag_name`, `name`, or `title`.
- `data["tname"]` as a category fallback.

Duplicate names are removed while preserving first-seen order.

## Repository

Add `VideoInfoSnapshotRepository.insert_from_parsed(parsed)`.

The repository only inserts one parsed snapshot and flushes the session. Query
commands for metadata are out of scope for this slice.

## Collector Flow

`VideoStatsCollector.collect()` should:

1. Fetch and archive raw payload once.
2. Decode the JSON once.
3. Parse metric counters and insert `video_metric_snapshots`.
4. Parse metadata and insert `video_info_snapshots`.
5. Return the existing successful coverage draft.

Both parsed rows should point at the same `raw_payload_id`.

## Testing

Required tests:

1. Parser maps title, description, owner, and mixed tag sources.
2. Parser deduplicates tag names and records source fields.
3. Repository inserts a `video_info_snapshots` row from parsed data.
4. Worker/collector writes both metric and info snapshots from one raw payload.
5. Malformed payload behavior remains covered by the existing parse failure
   worker test.

## Acceptance Criteria

- `video_info_snapshots` model exists and is created by `Base.metadata`.
- Video stats collection writes a metadata snapshot for successful tasks.
- Tags are stored as JSON with normalized `names`.
- TODO item `保存视频标题、简介、tag、UP 主信息快照。` is marked complete.
- `uv run pytest` and `uv run ruff check .` pass.

## Out Of Scope

- CLI query for video metadata snapshots.
- Video deletion, invisibility, or permission status tracking.
- Dynamic next snapshot scheduling.
- Backfilling old raw payloads into metadata snapshots.
