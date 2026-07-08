# Phase 1A Hot Comments Design

## Context

Books of Time is moving through Phase 1 of `docs/ROADMAP.md`: stable,
compliant, auditable collection of video metrics and comment snapshots. The
current baseline already supports video metric tasks, raw payload archival,
PostgreSQL-backed task leasing, and a custom `bilibili-api-python` request
backend that routes requests through the project rate limiter and raw evidence
pipeline.

This design implements the first comment collection slice from `docs/TODO.md`:
hot comment first-page collection with raw page evidence and structured comment
observations.

## Goal

Given a Bilibili BV id, enqueue and execute a `fetch_hot_comments` task that:

1. Resolves the video's numeric `aid` from the existing video info endpoint.
2. Fetches the first hot comment page through `bilibili-api-python`.
3. Archives the raw response through the existing raw payload store.
4. Records which target/page/sort the raw payload represents.
5. Upserts stable comment entities.
6. Appends per-run comment observations.

The user-facing acceptance target is:

```text
bot video comments BVxxxx --mode hot
uv run python main.py worker run-once
```

After the worker runs, the database contains raw payload evidence, a raw page
observation, stable comment entities, and observation rows for the hot comments
seen on page 1.

## Data Policy

Comment authors are **not anonymized** in this project. The system must keep the
public identifiers needed to inspect and verify collector behavior, including
author `mid` and public display name when present in the response.

Phase 1A still avoids user profiling:

- Store public author fields as evidence attached to public comments.
- Do not infer private identity.
- Do not label ordinary users.
- Do not build cross-event behavioral judgments.
- Do not hide collection limits or failed request windows.

## Text Hash Explanation

The system stores comment text and a content hash.

The hash is a deterministic fingerprint of the text, usually SHA-256 over a
normalized string. It is useful because the system can compare two observations
quickly:

- Same `rpid`, same content hash: text probably did not change.
- Same `rpid`, different content hash: text likely changed or was edited.

The hash does **not** replace the readable text. It is an indexable comparison
aid for later state events such as `content_changed`. To avoid ambiguous naming,
Phase 1A will use `first_content_hash` for the first observed text fingerprint
on `comment_entities`, and `content_hash` for each observation's current text.

## Scope

### In Scope

- Add ORM models:
  - `RawPageObservation`
  - `CommentEntity`
  - `CommentObservation`
- Add repositories for inserting page observations and upserting comments.
- Add parser for Bilibili hot comment payloads.
- Add `BilibiliPlatformClient.get_hot_comments(aid, page=1)`.
- Add `HotCommentCollector`.
- Register `TaskKind.FETCH_HOT_COMMENTS` in `build_worker`.
- Add CLI:
  - `bot video comments BVxxxx --mode hot`
- Add tests for parser, repository idempotence, platform request capture, worker
  execution, and CLI enqueue behavior where practical.
- Update `docs/TODO.md` checkboxes after implementation.

### Out of Scope

- Latest comment frontier scanning.
- Multi-page hot comment collection by video tier.
- Reply/root watchlist collection.
- Comment state event generation.
- Collection run and coverage summary tables.
- Request failure taxonomy and persistent backoff table.
- Frontend dashboard or report generation.

These are later Phase 1 slices.

## Interfaces

### CLI

Add a `video` command group:

```text
bot video comments BVxxxx --mode hot --priority 80
```

For Phase 1A, `--mode` accepts only `hot`. The command enqueues a
`TaskKind.FETCH_HOT_COMMENTS` task with:

```python
{
    "bvid": "BVxxxx",
    "mode": "hot",
    "page": 1,
}
```

The task target remains:

```python
target_type="video"
target_id=bvid
```

### Platform Client

Add:

```python
async def get_hot_comments(self, *, aid: int, page: int = 1) -> FetchResult:
    ...
```

The implementation uses:

```python
bilibili_api.comment.get_comments(
    oid=aid,
    type_=comment.CommentResourceType.VIDEO,
    page_index=page,
    order=comment.OrderType.LIKE,
)
```

The existing request classifier already maps comment reply requests with
`sort=2` to `BilibiliRequestType.COMMENT_HOT`.

### Collector

`HotCommentCollector.collect(task, session)`:

1. Reads `bvid` and `page` from task payload.
2. Fetches video info/stats through `client.get_video_stats(bvid)` to obtain
   `aid` when the task does not already carry `aid`.
3. Fetches hot comments through `client.get_hot_comments(aid=aid, page=page)`.
4. Saves the hot comment raw payload.
5. Inserts one `RawPageObservation`.
6. Parses comment rows.
7. Upserts one `CommentEntity` per `rpid`.
8. Appends one `CommentObservation` per parsed comment.

The extra video info request is acceptable for Phase 1A because it reuses the
existing auditable endpoint and keeps CLI ergonomics BV-based. Later phases can
cache or persist `aid` on a video table.

## Database Design

### `raw_page_observations`

Tracks the semantic meaning of a raw payload.

Fields:

- `id`: bigint primary key.
- `raw_payload_id`: bigint, references `raw_payloads.id` by convention.
- `captured_at`: UTC datetime.
- `request_type`: `BilibiliRequestType`.
- `target_type`: text, expected `video`.
- `target_id`: text, expected BV id.
- `page_number`: integer nullable, `1` for first hot page.
- `cursor`: text nullable, reserved for latest-comment cursor paging.
- `sort_mode`: text, `hot`.
- `parser_version`: string.
- `status`: text, `success` for parsed pages in Phase 1A.
- `item_count`: integer.
- `extra`: JSON dict for response cursor/count fields not worth columns yet.

Indexes:

- `(target_type, target_id, captured_at desc)`
- `(raw_payload_id)`

### `comment_entities`

Stores stable public comment identity and first-seen evidence.

Fields:

- `rpid`: bigint primary key.
- `oid`: bigint, Bilibili resource id, video `aid` for video comments.
- `bvid`: text.
- `root_rpid`: bigint nullable.
- `parent_rpid`: bigint nullable.
- `author_mid`: bigint nullable.
- `author_name`: text nullable.
- `first_content`: text nullable.
- `first_content_hash`: binary SHA-256.
- `first_seen_at`: UTC datetime.
- `first_raw_payload_id`: bigint nullable.
- `created_at`: UTC datetime.
- `updated_at`: UTC datetime.

Indexes:

- `(bvid, rpid)`
- `(author_mid)`

### `comment_observations`

Stores append-only observed comment state.

Fields:

- `id`: bigint primary key.
- `rpid`: bigint.
- `bvid`: text.
- `oid`: bigint.
- `captured_at`: UTC datetime.
- `raw_payload_id`: bigint nullable.
- `raw_page_observation_id`: bigint nullable.
- `sort_mode`: text, `hot`.
- `page_number`: integer nullable.
- `position`: integer nullable, one-based position within the parsed page.
- `content`: text nullable.
- `content_hash`: binary SHA-256.
- `like_count`: bigint nullable.
- `reply_count`: bigint nullable.
- `author_mid`: bigint nullable.
- `author_name`: text nullable.
- `is_deleted`: bool, default false.
- `visibility`: text, default `visible`.
- `extra`: JSON dict for fields such as member level, pendant, or future raw
  fragments.

Indexes:

- `(bvid, captured_at desc)`
- `(rpid, captured_at desc)`
- `(raw_page_observation_id)`

There is no uniqueness constraint on `(rpid, captured_at)` in Phase 1A because
two raw captures can occur close together. The task queue idempotence slice can
add stricter collection-run keys later.

## Parser Design

Create `books_of_time/parsers/comments.py`.

Public interface:

```python
COMMENT_PARSER_VERSION = "comments.v1"

@dataclass(frozen=True)
class ParsedComment:
    rpid: int
    oid: int
    bvid: str
    root_rpid: int | None
    parent_rpid: int | None
    author_mid: int | None
    author_name: str | None
    content: str | None
    content_hash: bytes
    like_count: int | None
    reply_count: int | None
    position: int

@dataclass(frozen=True)
class ParsedCommentPage:
    bvid: str
    oid: int
    captured_at: datetime
    raw_payload_id: int
    sort_mode: str
    page_number: int
    comments: list[ParsedComment]
    extra: dict[str, Any]

def parse_hot_comment_page(
    payload: dict[str, Any],
    *,
    bvid: str,
    oid: int,
    captured_at: datetime,
    raw_payload_id: int,
    page_number: int,
) -> ParsedCommentPage:
    ...
```

The parser reads `data.replies` as the main list. It should tolerate missing
fields by returning `None` for optional counts or author fields, but it should
raise a parse error when the payload is not a successful Bilibili API response
or does not contain a list-shaped replies field.

## Error Handling

Phase 1A keeps error handling aligned with the current worker:

- Collector exceptions mark the task for retry through existing
  `retry_count/not_before` behavior.
- Parse failures raise a specific parser exception type in the parser module,
  but persistent parse-error classification and `request_backoff_states` remain
  out of scope.
- HTTP status-specific backoff for 403, 429, captcha, and timeout is a later
  request-layer slice.

The collector must not write partial comment entities if parsing the page fails.
All writes occur inside the existing worker transaction.

## Testing Strategy

Add focused tests:

- Parser test with a minimal Bilibili-shaped payload containing two replies.
- Parser test for malformed successful payload with missing `replies`.
- Platform client test proving `get_hot_comments` routes through the custom
  Bili API client and uses `BilibiliRequestType.COMMENT_HOT`.
- Repository test proving the same `rpid` observed twice creates one
  `CommentEntity` and two `CommentObservation` rows.
- Worker test proving a `fetch_hot_comments` task archives raw data and writes
  page/entity/observation rows.
- CLI test can be added if the current CLI structure stays simple enough to
  exercise without a real database; otherwise, cover enqueue payload creation in
  the worker/repository tests and leave richer CLI tests for a CLI refactor.

Final verification:

```text
uv run pytest
uv run ruff check .
```

## Staged Delivery After Phase 1A

Phase 1A intentionally creates the tables and collector shape that the next
slices can reuse:

1. Latest comments frontier collection using `get_comments_lazy`.
2. `collection_runs` and coverage stats for requested/succeeded pages.
3. Worker loop, task list, retry-failed, and lease cleanup.
4. Request failure taxonomy and persistent backoff states.
5. Important replies and comment state events.

This keeps each delivery small enough to verify while continuing toward the
full Roadmap Phase 1 data foundation.

## Self-Review Notes

- No anonymization remains in the comment data model.
- Hash fields are defined as comparison aids and do not replace stored text.
- JSON extension fields use `extra`, avoiding SQLAlchemy's reserved
  `metadata` attribute name.
- Phase 1A is scoped to hot comment page 1; latest comments and coverage tables
  are explicitly later slices.
- The design keeps the existing project pattern: thin repositories, collectors
  for orchestration, parsers for payload normalization, and CLI for task enqueue.
