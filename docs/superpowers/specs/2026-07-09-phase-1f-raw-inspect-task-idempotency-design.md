# Phase 1F Raw Inspect And Task Idempotency Design

## Context

Phase 1 now has raw payload archiving, request backoff, worker loop, task
inspection, and coverage summaries. Two P0 foundations remain before the next
larger scheduler/discovery work:

- raw payloads can be saved, but there is no CLI to inspect an archived payload
  from its database id
- queue callers can enqueue duplicate active tasks for the same intent

Phase 1F closes both gaps without changing collection semantics.

## Goal

Add a small evidence-inspection CLI and active-task idempotency support.

The system must support:

1. `bot raw inspect <raw_payload_id>` to show raw metadata and a safe payload
   preview.
2. Repository support for reading a raw payload by id.
3. File-store support for reading and decompressing `file://...zst` payloads.
4. Optional task `idempotency_key`.
5. Duplicate active tasks with the same key return the existing task instead of
   inserting another row.

## Approved Design Constraints

- Raw inspect is read-only.
- Raw inspect prints a bounded text preview, not an unbounded dump.
- Raw inspect does not require a parser and does not mutate database state.
- Idempotency applies only to active tasks: `pending`, `running`, and `backoff`.
- `succeeded` and `failed` tasks do not block future enqueue with the same key.
- Callers may omit `idempotency_key`; omitted keys keep existing behavior.
- Do not introduce Redis or a new migration framework in this slice.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and
  `books_of_time/http/rate_limiter.py`.

## Raw Inspect CLI

Add:

```text
bot raw inspect <raw_payload_id> --preview-bytes 1200
```

Output one or more log lines containing:

- raw payload id
- request type
- captured time
- status code
- storage URI
- compressed and uncompressed sizes
- payload hash hex
- parser version
- decoded preview

Preview behavior:

- load raw body through `RawPayloadFileStore.read_uri(storage_uri)`
- decompress `.zst`
- decode as UTF-8 with replacement
- truncate to `preview_bytes`
- default `preview_bytes=1200`
- clamp CLI value to `0..10000`

If the database row does not exist, log `Raw payload not found: <id>`.

## Raw Repository And Store

Add repository method:

```python
async def get(self, raw_payload_id: int) -> RawPayload | None
```

Add file-store method:

```python
def read_uri(self, storage_uri: str) -> bytes
```

Supported storage:

- `file://...` paths written by the existing filesystem store

Unsupported schemes raise `ValueError`.

## Task Idempotency

Add nullable column:

```python
CollectionTask.idempotency_key: str | None
```

Repository API:

```python
async def enqueue(..., idempotency_key: str | None = None) -> CollectionTask
```

Semantics:

1. If `idempotency_key is None`, keep current insert behavior.
2. If a task exists with the same key and status in active states
   (`pending`, `running`, `backoff`), return that task.
3. Otherwise insert a new task with the key.

This is intentionally active-task idempotency, not permanent dedupe. A video can
still be sampled repeatedly after previous tasks succeed.

## Caller Keys

Use conservative stable keys for current enqueue call sites:

- manual video stats: `video-stats:{bvid}:manual`
- hot comments: `hot-comments:{bvid}:mode:{mode}`
- latest comments: `latest-comments:{bvid}:manual`
- latest follow-up pause/resume task:
  `latest-comments:{target_id}:resume:{cursor_or_manual}`
- fresh discovery stats:
  `video-stats:{bvid}:fresh-discovery`

Future scheduler work can include time-bucketed keys when repeated snapshots are
expected.

## Testing

Required tests:

1. Raw file store can read a saved `.zst` payload by URI.
2. Raw repository can fetch a raw payload by id.
3. `bot raw inspect` logs metadata and a bounded preview.
4. Same active idempotency key returns the existing task.
5. Same key after `succeeded` inserts a new task.
6. Current CLI enqueue helpers do not create duplicate active tasks.
7. Discovery scheduler keeps one active stat task per fresh video key.

## Acceptance Criteria

- `bot raw inspect <raw_payload_id>` exists and is covered by tests.
- Raw payload body can be read back from current filesystem storage.
- `CollectionTaskRepository.enqueue()` accepts `idempotency_key`.
- Active duplicate enqueue calls return the existing task.
- TODO items for raw inspect CLI and task idempotency are marked complete.
- `uv run pytest` and `uv run ruff check .` pass.

## Out Of Scope

- Raw reparse CLI.
- Failed-response raw archiving.
- Object storage backend.
- Alembic revision generation.
- Distributed exactly-once queue guarantees.
- Time-bucketed recurring snapshot scheduler keys.
