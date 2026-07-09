# Phase 1D Request Errors And Backoff Design

## Context

Phase 1C added per-task coverage rows, but request failures are still only
visible as generic collector exceptions. `docs/TODO.md` still lists three P0
request-layer gaps:

- unified request failure types: timeout, 403, 429, captcha, 5xx, parse_error
- `request_backoff_states` table
- worker and request-layer integration for failure backoff

Phase 1D fills those gaps without changing the project's compliance posture:
no proxy pools, no account pools, no captcha bypass.

## Goal

When a request or parser fails, the system records a stable error category,
updates request backoff state, delays affected tasks, and reports the category
through coverage instead of treating the failure as ordinary missing data.

The system must support:

1. Stable request error categories for `timeout`, `403`, `429`, `captcha`,
   `5xx`, and `parse_error`.
2. A durable `request_backoff_states` table keyed by platform, request type,
   and scope.
3. Request-layer classification of transport failures and HTTP responses.
4. Worker integration that records backoff state and delays retryable tasks.
5. Coverage rows whose failed reason is the stable error category.

## Approved Design Constraints

- Do not bypass platform risk controls.
- Do not retry captcha or 403 aggressively.
- Do not hide request failures as empty result sets.
- Keep raw successful-response archiving unchanged.
- If an HTTP response exists for a failed request, attach the `FetchResult` to
  the typed error so later slices can archive failed raw payloads.
- Use explicit string categories instead of parsing exception messages in the
  worker.
- Keep Phase 1D scoped to one worker task at a time. Global scheduler avoidance
  of backoff windows can be added after worker loop exists.

## Error Types

Add `books_of_time/http/errors.py`:

```python
class RequestErrorKind(StrEnum):
    TIMEOUT = "timeout"
    FORBIDDEN = "403"
    RATE_LIMITED = "429"
    CAPTCHA = "captcha"
    SERVER_ERROR = "5xx"
    PARSE_ERROR = "parse_error"
```

Add `RequestFailure`:

- `kind: RequestErrorKind`
- `request_type: BilibiliRequestType`
- `message: str`
- `status_code: int | None`
- `retry_after_seconds: int | None`
- `fetch_result: FetchResult | None`

Add `ParseFailure` as a subclass of `RequestFailure` that always uses
`RequestErrorKind.PARSE_ERROR`. Parser functions may continue raising native
errors internally, but collectors must wrap parse boundaries in Phase 1D and
raise `ParseFailure`.

## Request Classification

`RawHttpClient.request()` catches transport timeout exceptions and raises:

```python
RequestFailure(kind=RequestErrorKind.TIMEOUT, ...)
```

For HTTP responses, it still builds a `FetchResult`. A helper classifies the
response, and `RawHttpClient.request()` raises `RequestFailure` with
`fetch_result` attached when the response is a classified failure:

- status `403` -> `RequestErrorKind.FORBIDDEN`
- status `429` -> `RequestErrorKind.RATE_LIMITED`
- status `500..599` -> `RequestErrorKind.SERVER_ERROR`
- captcha/risk-control markers -> `RequestErrorKind.CAPTCHA`

Captcha detection is conservative. Phase 1D treats the response as captcha when
any of these are true:

- status code is `412`
- decoded body contains `captcha`
- decoded body contains `验证码`
- decoded body contains `风控`

If a failure response has a `Retry-After` header, parse it as seconds when it is
an integer. Otherwise the worker uses configured defaults.

The Bilibili API adapter catches `RequestFailure`, appends
`error.fetch_result` to the capture context when present, and re-raises the
same typed failure. This preserves request context for later failed-response
raw archiving.

## Backoff State

Add `request_backoff_states`:

- `id`: big integer primary key
- `platform`: text, initially `bilibili`
- `request_type`: `BilibiliRequestType`
- `scope`: text, initially `global`
- `error_kind`: text
- `status_code`: nullable integer
- `retry_after_seconds`: nullable integer
- `fail_count`: integer
- `first_failed_at`: UTC datetime
- `last_failed_at`: UTC datetime
- `backoff_until`: UTC datetime
- `last_message`: nullable text
- `extra`: JSON object
- `created_at`, `updated_at`: UTC datetimes

Unique key:

```text
platform, request_type, scope
```

Indexes:

- `(platform, request_type, scope)`
- `(backoff_until)`
- `(error_kind, last_failed_at DESC)`

## Backoff Policy

Add config:

```yaml
request_backoff:
  default_seconds:
    timeout: 60
    "403": 1800
    "429": 900
    captcha: 3600
    "5xx": 300
    parse_error: 300
  max_seconds: 21600
```

Policy:

- If the error has `retry_after_seconds`, use it.
- Otherwise use the configured default for the error kind.
- Multiply the base by `2 ** min(fail_count - 1, 5)`.
- Cap at `max_seconds`.
- A later success for the same request type may clear or reduce the backoff
  state in a future slice. Phase 1D only records failures and delays tasks.

## Worker Semantics

When a collector raises `RequestFailure` or `ParseFailure`:

1. Worker writes failed coverage with `reason=error.kind.value`.
2. Worker upserts `request_backoff_states`.
3. Worker increments `task.retry_count`.
4. If retries remain, task returns to `pending` with `not_before=backoff_until`.
5. If retries are exhausted, task becomes `failed`.
6. Worker re-raises the exception, preserving current `run_once()` failure
   behavior.

Generic non-request exceptions keep the existing `collector_exception` behavior
and default retry delay.

## Collector Parse Boundaries

Phase 1D wraps parser calls in current collectors:

- `VideoStatsCollector`: `parse_video_stats`
- `HotCommentCollector`: `_extract_aid`, `parse_hot_comment_page`
- `LatestCommentCollector`: aid extraction and `parse_latest_comment_page`

If parsing fails, raise `ParseFailure` with:

- `request_type` matching the raw payload being parsed
- `status_code` from the `FetchResult` when available
- message from the caught exception
- attached `fetch_result` when available

This makes parse failures visible in coverage and backoff state without
rewriting every parser in this slice.

## CLI And Inspection

Phase 1D does not add a new CLI. The existing `bot coverage BVxxxx` will show
failed coverage reason values such as `timeout`, `429`, or `parse_error`.

A future operations slice can add:

```text
bot request-backoff list
bot request-backoff clear ...
```

## Testing

Required tests:

1. Error classification maps timeout, 403, 429, captcha, 5xx, and parse_error
   to stable categories.
2. `request_backoff_states` repository creates and updates fail counts and
   `backoff_until`.
3. Worker catches `RequestFailure`, writes failed coverage using the error
   category, upserts backoff state, and delays the task to `backoff_until`.
4. Worker still treats generic exceptions as `collector_exception`.
5. A collector parse failure raises `ParseFailure` and records `parse_error`
   coverage/backoff.
6. Existing request and collector tests still pass.

## Acceptance Criteria

- `RequestFailure` and `ParseFailure` exist and carry stable error kinds.
- HTTP timeout and response classification is tested.
- `request_backoff_states` is represented in ORM and repository.
- Worker request failures update backoff state and task `not_before`.
- Failed coverage rows use stable error category reasons.
- Current collectors wrap parse boundaries as `parse_error`.
- `docs/TODO.md` marks the three P0 request failure/backoff items completed.
- `uv run pytest` and `uv run ruff check .` pass.

## Out Of Scope

- Proxy/account/captcha bypass.
- Failed-response raw payload archiving.
- Operator CLI for backoff states.
- Scheduler-level avoidance of active backoff windows before leasing tasks.
- Event-level quality reports.
