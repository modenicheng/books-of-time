# Phase 1B Latest Comments Frontier Design

## Context

Phase 1A added hot comment first-page collection, public comment identity
storage, append-only comment observations, raw page observations, and
`bot video comments BVxxxx --mode hot`.

Phase 1B implements the latest-comment side of the Phase 1 data foundation. It
must not treat a fixed number of pages as an initial full snapshot. The first
scan establishes a recoverable baseline over multiple short worker runs, and
later scans use a frontier to collect only new comments.

## Goal

Given a Bilibili BV id, collect latest comments in timestamp order through the
new cursor-based comment API while preserving raw evidence and clear coverage
state.

The system must support:

1. A first baseline scan that walks cursor pages until the service reports an
   end, pausing and resuming across worker runs when the time slice expires.
2. A final head sweep after baseline completion to capture comments created
   during the baseline window.
3. Later incremental scans that stop when they reach the previous
   `frontier_rpid`.
4. Explicit `paused`, `complete`, `truncated`, and `corrupted` states so reports
   never confuse partial coverage with a full baseline.

## Approved Design Constraints

- `page_limit` must not be used to mean "total comments per request"; the
  Bilibili lazy comments API does not expose a page-size parameter.
- A single collector run must keep outbound requests within a one-minute update
  window. The default request time slice is 55 seconds so the worker can commit
  and schedule follow-up work before the next minute.
- Page-level request failure is retried with configurable attempts and backoff.
- If the same page/cursor fails after all configured attempts, the scan stops
  and is marked `corrupted`, not `complete`.
- If the time slice expires without a failed page, the scan is marked `paused`
  and can resume from the saved cursor.
- Retry attempts and retry sleeps are also bounded by `max_scan_seconds`. If a
  page has failed but the current run reaches the time slice before exhausting
  configured attempts, the collector records the failed cursor and attempt count
  as paused state, then resumes retrying that same cursor in a later run.
- Comment authors remain non-anonymized. Store public `mid` and display name
  when present.
- Store readable comment content and `content_hash`; the hash is only a
  comparison aid.

## API Reality

`bilibili_api.comment.get_comments_lazy()` has this shape:

```python
get_comments_lazy(
    oid: int,
    type_: comment.CommentResourceType,
    offset: str = "",
    order: comment.OrderType = comment.OrderType.TIME,
)
```

It returns a cursor page. The next request uses:

```python
payload["cursor"]["pagination_reply"]["next_offset"]
```

There is no client-side `page_size` parameter in the library. Any effective
limit comes from Bilibili service behavior, credential state, and risk controls.

## Baseline Semantics

The first latest-comment collection for a video creates a baseline. A baseline
is not a single instant snapshot. It is a documented observation window:

```text
baseline window = baseline_started_at .. baseline_completed_at
```

During baseline:

1. Capture the first page and remember its newest comment as
   `baseline_start_frontier_rpid`.
2. Follow `next_offset` until the service returns no more pages, no replies, or
   an explicit end marker.
3. If the 55-second time slice expires, save the current cursor and mark the
   state `baseline_paused`.
4. A later worker run resumes from the saved cursor.
5. After the tail is reached, perform a head sweep from the first page until
   `baseline_start_frontier_rpid` is reached. This captures comments created
   during the baseline window.
6. Only after the tail scan and head sweep complete does the system mark
   `baseline_complete` and set the official frontier to the newest first-page
   comment from the head sweep.

If any page repeatedly fails after configured retry attempts, the baseline is
marked `baseline_corrupted`. Corrupted baselines are evidence-bearing partial
data, but they must not be treated as a complete t0 state.

## Incremental Semantics

After baseline completion, later latest-comment scans are incremental:

1. Start from the first latest-comments page.
2. Save each raw payload and raw page observation.
3. Write comment entities and observations.
4. Stop when the previous `frontier_rpid` is seen.
5. Set the new frontier to the newest comment observed on this run.

If the old frontier is not seen:

- If the service end is reached, mark `last_scan_status="frontier_missing"` and
  `last_scan_truncated=false`. This means the previous frontier may have been
  deleted, folded, or moved outside the reachable result set.
- If the run pauses because of the 55-second time slice, mark
  `last_scan_status="paused"` and `last_scan_truncated=true`.
- If a page fails after retries or a cursor loop is detected, mark
  `last_scan_status="corrupted"` and `last_scan_truncated=true`.

## Duplicate And Pagination Stability

Cursor paging is not a database snapshot. New comments can arrive while a scan
is running, and deleted or folded comments can change what the cursor returns.

The collector handles this by:

- Recording every requested cursor in the current scan. If a cursor repeats,
  stop and mark the scan `corrupted` to avoid an infinite loop.
- Upserting `comment_entities` by `rpid`, so comment identity is not duplicated.
- Appending `comment_observations` as evidence of what was seen at a capture
  time.
- Using `raw_page_observations` to record cursor, page sequence, sort mode, item
  count, and parser version.

Phase 1B does not add full `collection_runs` tables. To support resumable
baseline without pulling Phase 1C forward too far, it adds one lightweight JSON
extension field, `frontier_states.extra`, and keeps coverage summary tables out
of scope.

## Data Model Changes

The existing `frontier_states` table has most of the required columns:

- `target_type`
- `target_id`
- `frontier_type`
- `frontier_rpid`
- `frontier_time`
- `cursor`
- `last_scan_at`
- `last_scan_status`
- `last_scan_pages`
- `last_scan_truncated`

Phase 1B adds one JSON extension column named `extra` to `frontier_states` for
baseline-specific state:

- `baseline_started_at`
- `baseline_completed_at`
- `baseline_start_frontier_rpid`
- `baseline_start_frontier_time`
- `baseline_status`
- `failed_cursor`
- `failed_reason`
- `failed_attempts`
- `seen_cursors`

Do not encode these values into existing text columns. Do not add `metadata`;
it is a SQLAlchemy reserved attribute name.

## Configuration

Add `latest_comments` configuration:

```yaml
latest_comments:
  max_scan_seconds: 55
  page_retry_attempts: 3
  page_retry_backoff_seconds: [1, 3, 5]
```

Meanings:

- `max_scan_seconds`: maximum outbound-request time budget for one collector
  run. Default 55 seconds.
- `page_retry_attempts`: number of attempts for the same cursor before marking
  the scan corrupted. Default 3.
- `page_retry_backoff_seconds`: per-attempt sleep schedule. If attempts exceed
  the list length, reuse the last value.

These settings are separate from worker task retry. Worker retry handles a whole
task failure; page retry handles one cursor request inside a scan.

## Platform Client

Add:

```python
async def get_latest_comments(
    self,
    *,
    aid: int,
    offset: str = "",
) -> FetchResult:
    ...
```

Implementation uses:

```python
comment.get_comments_lazy(
    oid=aid,
    type_=comment.CommentResourceType.VIDEO,
    offset=offset,
    order=comment.OrderType.TIME,
)
```

The request classifier already treats `mode=2` as `BilibiliRequestType.COMMENT_LATEST`.

## Parser

Extend `books_of_time/parsers/comments.py` with:

```python
def parse_latest_comment_page(
    payload: dict[str, Any],
    *,
    bvid: str,
    oid: int,
    captured_at: datetime,
    raw_payload_id: int,
    page_number: int,
    request_offset: str,
) -> ParsedCommentPage:
    ...
```

The returned page uses:

- `sort_mode="latest"`
- `extra["request_offset"]`
- `extra["next_offset"]`
- `extra["is_end"]` when available

The parser should accept empty or `None` replies as an end page only when the
payload shape is otherwise valid. Malformed cursor structures raise
`CommentParseError`.

## Collector

Add `LatestCommentCollector`.

Responsibilities:

1. Resolve `aid` from task payload or video info.
2. Load or create `FrontierState(target_type="video", target_id=bvid,
   frontier_type="latest_comments")`.
3. Determine scan mode:
   - no baseline complete -> baseline tail scan or baseline resume
   - tail complete but head sweep incomplete -> head sweep
   - baseline complete -> incremental scan
4. Request pages until one stop condition:
   - service end
   - previous frontier reached
   - baseline start frontier reached during head sweep
   - max scan seconds reached
   - page retry attempts exhausted
   - repeated cursor detected
5. Save raw payloads, raw page observations, comment entities, and comment
   observations for each successful page.
6. Update `FrontierState` status and cursor.
7. Enqueue a follow-up `fetch_latest_comments` task when the scan is paused.

## CLI

Add:

```text
bot collect-latest-comments BVxxxx
```

Optional flags:

```text
--priority 70
--max-scan-seconds 55
```

The CLI enqueues `TaskKind.FETCH_LATEST_COMMENTS` with:

```python
{
    "bvid": bvid,
    "mode": "latest",
}
```

The collector reads defaults from config and allows task payload overrides only
for tests or explicit CLI flags.

## Status Values

Use these `frontier_states.last_scan_status` values:

- `baseline_paused`
- `baseline_tail_complete`
- `baseline_complete`
- `baseline_corrupted`
- `incremental_complete`
- `frontier_missing`
- `paused`
- `corrupted`

Use `last_scan_truncated=true` for paused/corrupted scans and for any scan that
cannot prove it reached the intended boundary.

## Error Handling

Page request failure:

1. Retry the same cursor using `page_retry_attempts`.
2. Sleep according to `page_retry_backoff_seconds`.
3. Before each retry sleep and before each next request, check
   `max_scan_seconds`.
4. If the current run's time slice expires before attempts are exhausted, save
   `failed_cursor`, `failed_reason`, and `failed_attempts`, mark the scan
   paused/truncated, and enqueue a follow-up task.
5. If attempts reach `page_retry_attempts`, mark the scan corrupted and stop.

Worker-level failure:

- Unexpected exceptions still flow through the existing worker retry mechanism.
- The collector should prefer recording corrupted scan state for known page
  failures instead of raising after all page attempts are exhausted.

Service cap or credential limitation:

- If the service returns an otherwise valid end page, mark the scan according to
  the boundary it reached.
- If evidence suggests the service capped the result before the tail, record the
  reason in frontier state and do not mark baseline complete.

## Testing Strategy

Tests must cover:

- Latest parser extracts comments and next offset.
- Latest parser treats valid empty replies as end.
- Platform client captures latest comments as `BilibiliRequestType.COMMENT_LATEST`.
- Baseline pauses at the configured time budget and saves cursor.
- Baseline resumes from saved cursor and eventually marks tail complete.
- Head sweep completes baseline and sets the official frontier.
- Incremental scan stops at old frontier and updates to new frontier.
- Missing old frontier at service end is `frontier_missing`, not a silent
  success.
- Same cursor repeated marks scan `corrupted`.
- Same cursor request failing after configured attempts marks scan
  `baseline_corrupted` or `corrupted`.
- Page request failure followed by time-slice expiry before attempts are
  exhausted marks the scan paused and resumes the same cursor next run.
- CLI enqueues `fetch_latest_comments`.

Final verification:

```text
uv run pytest
uv run ruff check .
```

## Scope Boundaries

In scope:

- Latest comment parser.
- Latest comment platform request method.
- Resumable baseline state.
- 55-second default scan time budget.
- Configurable page retry attempts and backoff.
- Latest comment collector.
- CLI task enqueue.
- TODO update.

Out of scope:

- Full `collection_runs` and coverage summary tables.
- Reply collection.
- Comment state event generation.
- Request-layer 403/429/captcha taxonomy.
- Dashboard/report UI.
- Strong claims of an atomic instant t0 snapshot.

## Self-Review Notes

- The spec no longer uses fixed N pages as a baseline completion condition.
- `max_scan_seconds` is a collector run budget, not a server page size.
- `corrupted` is reserved for repeated page failure, cursor loop, or malformed
  evidence chain.
- Time budget expiry produces a resumable paused state, not corrupted.
- Retry attempts never extend one collector run beyond `max_scan_seconds`; failed
  cursor retry state is persisted across runs.
- The baseline definition is an observation window, not an atomic service
  snapshot.
- Public user fields remain stored for verification.
