# Event Archive Design

## Goal

Organize collected videos, comments, targets, and evidence under explicit research events without making unsupported relevance claims. Operators can create an event, add public UID/keyword/seed-video/game targets, inspect associated videos, summarize coverage, and export a basic evidence timeline.

## Data Model

### `events`

- Integer primary key and unique human-readable `slug`.
- Name, game, description, status, optional start/end window, and timezone.
- Status values are `planned`, `active`, `closed`, and `archived`.
- Event records are mutable metadata; collected observations remain append-only.

### `event_targets`

- Belongs to one event.
- Types: `uid`, `keyword`, `seed_bvid`, and `game`.
- Stores original public value plus a normalized value used for uniqueness.
- Stores priority, active flag, first/last seen timestamps, and JSON metadata.
- Unique on `(event_id, target_type, normalized_value)`.

UID normalization strips surrounding whitespace and canonicalizes decimal IDs.
Keywords and games use Unicode casefold plus whitespace collapse. BVID values are
trimmed but preserve their canonical public spelling.

### `event_videos`

- Composite identity `(event_id, bvid)`.
- Stores first/last association timestamps, reason, optional source target,
  confidence, and active flag.
- Association reasons are explicit: `seed_bvid`, `uid_discovery`, `manual`, or
  later `keyword_candidate`.
- UID and seed associations use confidence `1.0`. Keyword search results are not
  automatically promoted to event videos in the first release.

### `event_keywords`

- Versioned event vocabulary with original and normalized text.
- Stores category, version, active flag, and optional source target.
- Adding a keyword target creates the first active keyword row in the same
  transaction.

## Repository API

`EventRepository` owns event metadata and cross-table invariants:

- `create_event(...)`
- `resolve_event(id_or_slug)`
- `list_events(...)`
- `add_target(...)`
- `attach_video(...)`
- `list_videos(...)`
- `coverage_summary(...)`
- `build_timeline(...)`

Duplicate create/add operations return the existing record when their stable key
matches. Conflicting event slugs fail explicitly.

## CLI

```text
bot event create NAME --slug SLUG --game GAME
bot event list
bot event add-target EVENT --type uid|keyword|seed_bvid|game VALUE
bot event list-videos EVENT
bot event coverage EVENT
bot event export-timeline EVENT --output PATH
```

`EVENT` accepts numeric ID or slug. CLI output always includes stable IDs and
association reasons. Public UID/BVID values remain visible for verification.

Adding a `seed_bvid` target immediately attaches the video and enqueues an
idempotent video-stats task. It does not fabricate publication metadata.

## Scheduler Integration

The UID discovery scheduled handler loads active UID targets from active events
on every execution and enqueues normal `DISCOVER_USER_VIDEOS` tasks with
`source_pool_type=event` and `source_pool_id=<event_id>`.

When the discovery collector sees this source metadata, every parsed video is
attached to that event with reason `uid_discovery`. Existing config-based UID
pools continue to work; a pool ID that does not resolve to an event simply does
not create event associations.

Keyword targets are archived and available to later search analysis, but do not
automatically associate videos. This prevents weak textual matches from becoming
asserted event membership.

## Coverage And Timeline

Event coverage aggregates collection coverage for associated BVIDs: video count,
run count, requested/succeeded pages, observed items, raw payload count, request
errors, and parse errors.

The first JSONL timeline contains event-video associations, video metric
snapshots, comment state events, and comment visibility events for associated
videos. Every row includes source table/type, timestamp, stable identifiers, and
raw/observation references when available. Export does not alter the database.

## Error Handling

- Unknown event identifiers fail without creating targets.
- Invalid UID, empty keyword/game, malformed BVID, invalid time window, or
  unsupported target type fail before database mutation.
- Scheduler task idempotency uses event ID, UID, and scheduled slot.
- Discovery request failures remain ordinary worker failures with raw/coverage
  semantics; they do not remove prior event associations.

## Delivery

1. Event Core: four tables, repository, create/list/add-target/list-videos CLI.
2. Event Discovery: active UID targets and event-video association.
3. Event Evidence: coverage summary and JSONL timeline export.

## Acceptance

- Duplicate targets do not create duplicate rows.
- A seed BVID is immediately visible in event videos and queues one stats task.
- A scheduled event UID discovery attaches returned videos to the correct event.
- Event coverage equals the aggregate of its associated video coverage rows.
- Timeline rows are chronologically ordered and preserve evidence identifiers.
