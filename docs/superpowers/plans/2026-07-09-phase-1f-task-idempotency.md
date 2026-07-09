# Phase 1F Raw Inspect And Task Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add raw payload inspection and active-task idempotency so evidence can be checked from CLI and duplicate active queue work is avoided.

**Architecture:** Extend existing repositories and filesystem storage with read-side APIs, then add a thin `bot raw inspect` CLI helper. Extend `collection_tasks` with optional `idempotency_key` and have `CollectionTaskRepository.enqueue()` return an existing active task for the same key.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, argparse CLI, zstandard, pytest-asyncio, Ruff.

## Global Constraints

- Raw inspect is read-only.
- Raw inspect prints a bounded text preview, not an unbounded dump.
- Raw inspect does not require a parser and does not mutate database state.
- Idempotency applies only to active tasks: `pending`, `running`, and `backoff`.
- `succeeded` and `failed` tasks do not block future enqueue with the same key.
- Callers may omit `idempotency_key`; omitted keys keep existing behavior.
- Do not introduce Redis or a new migration framework in this slice.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and `books_of_time/http/rate_limiter.py`.
- Execute inline in this main session; do not dispatch subagents unless the user asks again.

---

## File Structure

- Modify `books_of_time/storage/filesystem.py`: add `RawPayloadFileStore.read_uri()`.
- Modify `books_of_time/db/repositories.py`: add `RawPayloadRepository.get()` and `CollectionTaskRepository.enqueue(..., idempotency_key=None)`.
- Modify `books_of_time/db/models.py`: add `CollectionTask.idempotency_key` and an active partial unique index.
- Modify `books_of_time/cli.py`: add `raw inspect` command and idempotency keys for manual enqueue helpers.
- Modify `books_of_time/task_orchestrator/discovery.py`: pass an idempotency key for fresh discovery stat tasks.
- Modify `books_of_time/collectors/latest_comments.py`: pass an idempotency key for paused/resume follow-up tasks.
- Modify `tests/test_raw_storage.py`: filesystem read test.
- Modify `tests/test_cli.py`: raw inspect and duplicate CLI enqueue tests.
- Modify `tests/test_task_queue.py`: idempotency repository tests.
- Modify `tests/test_discovery_scheduler.py`: discovery idempotency expectation.
- Modify `docs/TODO.md`: mark raw inspect CLI and task idempotency complete.

---

### Task 1: Raw Payload Read And Inspect

**Files:**
- Modify: `books_of_time/storage/filesystem.py`
- Modify: `books_of_time/db/repositories.py`
- Modify: `books_of_time/cli.py`
- Test: `tests/test_raw_storage.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `RawPayloadFileStore.read_uri(storage_uri: str) -> bytes`.
- Produces: `RawPayloadRepository.get(raw_payload_id: int) -> RawPayload | None`.
- Produces: CLI helper `_inspect_raw_payload(cfg: dict, raw_payload_id: int, preview_bytes: int) -> None`.

- [ ] **Step 1: Write failing tests**

Add a storage test that saves a body and reads it back:

```python
def test_raw_payload_file_store_reads_saved_uri(tmp_path) -> None:
    store = RawPayloadFileStore(tmp_path)
    stored = store.save(
        body=b'{"hello":"world"}',
        captured_at=datetime(2099, 1, 1, tzinfo=UTC),
        run_id="run-1",
        suffix=".json",
    )

    assert store.read_uri(stored.storage_uri) == b'{"hello":"world"}'
```

Add CLI parser and helper tests:

```python
def test_raw_inspect_parser_accepts_payload_id() -> None:
    args = build_parser().parse_args(["raw", "inspect", "123", "--preview-bytes", "20"])

    assert args.command == "raw"
    assert args.raw_command == "inspect"
    assert args.raw_payload_id == 123
    assert args.preview_bytes == 20
```

```python
async def test_inspect_raw_payload_logs_metadata_and_preview(tmp_path, caplog) -> None:
    cfg = {
        "database": {"url": f"sqlite+aiosqlite:///{tmp_path / 'raw.sqlite3'}"},
        "storage": {"raw_dir": str(tmp_path / "raw")},
    }
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    store = RawPayloadFileStore(tmp_path / "raw")
    captured_at = datetime(2099, 1, 1, tzinfo=UTC)
    stored = store.save(
        body=b'{"message":"hello raw inspect"}',
        captured_at=captured_at,
        run_id="run-1",
        suffix=".json",
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        raw = await RawPayloadRepository(session).insert_from_fetch_result(
            result=FetchResult(
                request_type=BilibiliRequestType.VIDEO_STATS,
                method="GET",
                url="https://api.bilibili.com/x/web-interface/view",
                params={"bvid": "BV1"},
                status_code=200,
                body=b'{"message":"hello raw inspect"}',
                captured_at=captured_at,
                response_headers={},
            ),
            stored=stored,
            parser_version="test",
        )
        raw_id = raw.id
        await session.commit()
    await engine.dispose()

    await cli._inspect_raw_payload(cfg, raw_id, preview_bytes=12)

    assert f"raw id={raw_id}" in caplog.text
    assert "bilibili:video_stats" in caplog.text
    assert "hello raw" in caplog.text
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_raw_storage.py tests/test_cli.py -v
```

Expected: FAIL because `read_uri`, raw repository get, parser command, or CLI helper is missing.

- [ ] **Step 3: Implement raw inspect**

Implement `read_uri()` for `file://` URIs using `zstandard.ZstdDecompressor().decompress(...)`. Add `RawPayloadRepository.get()`. Add parser branch `raw inspect` and `_inspect_raw_payload()` that clamps preview bytes to `0..10000`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_raw_storage.py tests/test_cli.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/storage/filesystem.py books_of_time/db/repositories.py books_of_time/cli.py tests/test_raw_storage.py tests/test_cli.py
git commit -m "feat: add raw payload inspect cli"
```

---

### Task 2: Repository Task Idempotency

**Files:**
- Modify: `books_of_time/db/models.py`
- Modify: `books_of_time/db/repositories.py`
- Test: `tests/test_task_queue.py`

**Interfaces:**
- Consumes: `TaskStatus.PENDING`, `TaskStatus.RUNNING`, `TaskStatus.BACKOFF`.
- Produces: `CollectionTask.idempotency_key`.
- Produces: `CollectionTaskRepository.enqueue(..., idempotency_key: str | None = None) -> CollectionTask`.

- [ ] **Step 1: Write failing tests**

Add tests proving active reuse and post-success re-enqueue:

```python
async def test_task_repository_reuses_active_task_with_same_idempotency_key() -> None:
    first = await repo.enqueue(..., idempotency_key="fetch_video_stats:video:BVDEDUP")
    second = await repo.enqueue(..., idempotency_key="fetch_video_stats:video:BVDEDUP")
    assert second.id == first.id
```

```python
async def test_task_repository_allows_reenqueue_after_success() -> None:
    first = await repo.enqueue(..., idempotency_key="fetch_video_stats:video:BVDEDUP")
    first.status = TaskStatus.SUCCEEDED
    second = await repo.enqueue(..., idempotency_key="fetch_video_stats:video:BVDEDUP")
    assert second.id != first.id
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_task_queue.py -v
```

Expected: FAIL before repository idempotency is implemented.

- [ ] **Step 3: Implement model and repository behavior**

Add nullable `idempotency_key` and a partial unique active index. In `enqueue()`, when a key is provided, query active tasks ordered by oldest first and return the existing task before inserting.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_task_queue.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/db/models.py books_of_time/db/repositories.py tests/test_task_queue.py
git commit -m "feat: add task idempotency keys"
```

---

### Task 3: Enqueue Callers And TODO Sync

**Files:**
- Modify: `books_of_time/cli.py`
- Modify: `books_of_time/task_orchestrator/discovery.py`
- Modify: `books_of_time/collectors/latest_comments.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_discovery_scheduler.py`
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: `CollectionTaskRepository.enqueue(..., idempotency_key=...)`.

- [ ] **Step 1: Write or update failing caller tests**

Add tests proving duplicate manual enqueue and discovery enqueue produce one active task for the same key.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_cli.py tests/test_discovery_scheduler.py -v
```

Expected: FAIL until callers pass idempotency keys.

- [ ] **Step 3: Implement caller keys**

Pass keys:

- `video-stats:{bvid}:manual`
- `hot-comments:{bvid}:mode:{mode}`
- `latest-comments:{bvid}:manual`
- `latest-comments:{target_id}:resume:{cursor_or_manual}`
- `video-stats:{bvid}:fresh-discovery`

Mark TODO raw inspect and task idempotency items complete.

- [ ] **Step 4: Full verification**

Run:

```bash
uv run pytest
uv run ruff check .
```

Expected: all tests pass and Ruff reports no issues.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/cli.py books_of_time/task_orchestrator/discovery.py books_of_time/collectors/latest_comments.py tests/test_cli.py tests/test_discovery_scheduler.py docs/TODO.md
git commit -m "feat: deduplicate task enqueue callers"
```

---

## Self-Review

- Spec coverage: raw inspect CLI, raw file read, raw repository get, task idempotency, caller keys, TODO sync, and verification are covered.
- Placeholder scan: no TBD/fill-later placeholders are present.
- Type consistency: method names match the design document and existing repository patterns.
