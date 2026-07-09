# Phase 1J Video Availability Implementation Plan

> **Execution mode:** Implement inline in this main session. Avoid opening subagents unless the user explicitly asks for them again. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record per-BV availability snapshots for visible, deleted, invisible, permission-denied, and unknown business-error video payloads.

**Architecture:** Add a parser/dataclass for availability classification, persist it in a dedicated append-only table, then make `VideoStatsCollector` write availability before deciding whether metric/info parsing should continue.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, pytest-asyncio, Ruff.

## Global Constraints

- Keep platform/global failures in `RequestBackoffState`; this slice adds target-level availability records.
- Use a separate `video_availability_snapshots` table instead of overloading metrics or info snapshots.
- Insert a `visible` availability row for normal payloads.
- For known target-unavailable payloads, insert availability and finish the collection task without writing metric/info snapshots.
- Do not add new Bilibili API requests.
- Do not anonymize owner or video identifiers.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and `books_of_time/http/rate_limiter.py`.
- Execute inline in this main session; do not dispatch subagents unless the user asks again.

---

## File Structure

- Modify `books_of_time/parsers/video.py`: add `ParsedVideoAvailabilitySnapshot` and `parse_video_availability_snapshot()`.
- Modify `tests/test_video_stats.py`: add availability parser tests.
- Modify `books_of_time/db/models.py`: add `VideoAvailabilitySnapshot` and indexes.
- Modify `books_of_time/db/repositories.py`: add `VideoAvailabilitySnapshotRepository`.
- Modify `books_of_time/db/__init__.py`: export `VideoAvailabilitySnapshot`.
- Modify `tests/test_video_stats_worker.py`: add repository coverage plus visible/deleted worker assertions.
- Modify `books_of_time/collectors/video_stats.py`: write availability before metric/info parsing and short-circuit unavailable payloads.
- Modify `docs/TODO.md`: mark the availability TODO complete.

---

### Task 1: Availability Parser

**Files:**
- Modify: `books_of_time/parsers/video.py`
- Test: `tests/test_video_stats.py`

**Interfaces:**
- Produces: `ParsedVideoAvailabilitySnapshot`.
- Produces: `parse_video_availability_snapshot(payload, *, captured_at, raw_payload_id, requested_bvid, http_status_code)`.

- [ ] **Step 1: Write failing parser tests**

Add tests covering normal and error payloads:

```python
def test_parse_video_availability_snapshot_marks_visible_payload() -> None:
    snapshot = parse_video_availability_snapshot(
        {"code": 0, "message": "OK", "data": {"bvid": "BV1abc"}},
        captured_at=captured_at,
        raw_payload_id=42,
        requested_bvid="BVfallback",
        http_status_code=200,
    )
    assert snapshot.bvid == "BV1abc"
    assert snapshot.status == "visible"
```

```python
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"code": -404, "message": "稿件不存在"}, "deleted"),
        ({"code": -403, "message": "权限不足"}, "permission_denied"),
        ({"code": -1, "message": "稿件不可见"}, "invisible"),
        ({"code": -500, "message": "unknown"}, "unknown_error"),
    ],
)
def test_parse_video_availability_snapshot_classifies_business_errors(payload, expected):
    snapshot = parse_video_availability_snapshot(
        payload,
        captured_at=captured_at,
        raw_payload_id=42,
        requested_bvid="BV1abc",
        http_status_code=200,
    )
    assert snapshot.bvid == "BV1abc"
    assert snapshot.status == expected
```

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/test_video_stats.py -v`.

Expected: FAIL because `parse_video_availability_snapshot` does not exist.

- [ ] **Step 3: Implement parser**

Classify by business code/message, using conservative keyword checks for deleted, permission, and invisible states.

- [ ] **Step 4: Verify GREEN**

Run `uv run pytest tests/test_video_stats.py -v`.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/parsers/video.py tests/test_video_stats.py
git commit -m "feat: parse video availability snapshots"
```

---

### Task 2: Availability Persistence

**Files:**
- Modify: `books_of_time/db/models.py`
- Modify: `books_of_time/db/repositories.py`
- Modify: `books_of_time/db/__init__.py`
- Test: `tests/test_video_stats_worker.py`

**Interfaces:**
- Produces: `VideoAvailabilitySnapshot` ORM model.
- Produces: `VideoAvailabilitySnapshotRepository.insert_from_parsed(parsed)`.

- [ ] **Step 1: Write failing repository test**

Add a test that parses a visible payload, inserts it through the repository, and retrieves it by composite primary key.

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/test_video_stats_worker.py::test_video_availability_snapshot_repository_inserts_parsed_snapshot -v`.

Expected: FAIL because the model/repository does not exist.

- [ ] **Step 3: Implement model, export, and repository**

Add the table with `bvid`, `captured_at`, `status`, `bili_code`, `bili_message`, `http_status_code`, and `raw_payload_id`.

- [ ] **Step 4: Verify GREEN**

Run the same single test.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/db/models.py books_of_time/db/repositories.py books_of_time/db/__init__.py tests/test_video_stats_worker.py
git commit -m "feat: persist video availability snapshots"
```

---

### Task 3: Collector Integration

**Files:**
- Modify: `books_of_time/collectors/video_stats.py`
- Modify: `tests/test_video_stats_worker.py`
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: `parse_video_availability_snapshot(...)`.
- Consumes: `VideoAvailabilitySnapshotRepository.insert_from_parsed(...)`.
- Produces: worker runs that write visible availability with metric/info rows, and unavailable availability without metric/info rows.

- [ ] **Step 1: Write failing worker tests**

Extend the existing successful worker test to assert a `visible` availability row. Add a deleted-payload fake client and test that the worker succeeds, records `deleted`, and writes no metric/info snapshots.

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/test_video_stats_worker.py -v`.

Expected: FAIL because collector does not write availability rows.

- [ ] **Step 3: Implement collector integration**

After raw archival, decode once, insert availability, short-circuit non-visible statuses with coverage reason equal to the status, and keep visible parsing behavior unchanged.

- [ ] **Step 4: Verify GREEN**

Run `uv run pytest tests/test_video_stats_worker.py -v`.

Expected: PASS.

- [ ] **Step 5: Full verification**

Run:

```bash
uv run pytest
uv run ruff check .
```

Expected: all tests pass and Ruff reports no issues.

- [ ] **Step 6: Commit**

```bash
git add books_of_time/collectors/video_stats.py tests/test_video_stats_worker.py docs/TODO.md
git commit -m "feat: collect video availability snapshots"
```

---

## Self-Review

- Spec coverage: parser classification, table persistence, collector visible/unavailable paths, TODO update, and full verification are covered.
- Placeholder scan: no `TBD` or unspecified implementation steps.
- Type consistency: names are consistent across parser, repository, model, and collector tasks.
