# Phase 1I Video Info Snapshots Implementation Plan

> **Execution mode:** Implement inline in this main session. Avoid opening subagents unless the user explicitly asks for them again.

**Goal:** Save title, description, tag/category names, and UP owner snapshots from the existing video stats payload.

**Architecture:** Reuse the archived `Video.get_info()` payload already fetched by `VideoStatsCollector`. Add a parser/dataclass, an append-only `video_info_snapshots` model and repository, then insert the metadata snapshot beside the existing metric snapshot in the same worker flow.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, SQLite/PostgreSQL-compatible JSON column, pytest-asyncio, Ruff.

## Global Constraints

- Use the existing `get_video_stats()` request; do not add another Bilibili API request in this slice.
- Keep snapshots append-only, keyed by `bvid` and `captured_at`, matching `video_metric_snapshots`.
- Store user and owner data without anonymization so operators can verify system behavior.
- Store tags in a JSON object so the parser can keep both normalized names and lightweight source hints without schema churn.
- Missing optional metadata fields should be stored as `None` or empty tag names, not treated as parse failure.
- Missing `data.bvid` remains a parse failure because the row cannot be keyed.
- Preserve unrelated dirty changes in `books_of_time/http/client.py`, `books_of_time/http/rate_limiter.py`, `books_of_time/cli.py`, and `tests/test_cli.py` unless this slice explicitly needs those files.
- Execute inline in this main session; do not dispatch subagents unless the user asks again.

---

## File Structure

- Modify `books_of_time/parsers/video.py`: add `ParsedVideoInfoSnapshot`, tag normalization helpers, and `parse_video_info_snapshot()`.
- Modify `tests/test_video_stats.py`: add parser tests for metadata and tag normalization.
- Modify `books_of_time/db/models.py`: add `VideoInfoSnapshot` and `idx_video_info_snapshots_bvid_time`.
- Modify `books_of_time/db/repositories.py`: add `VideoInfoSnapshotRepository.insert_from_parsed()`.
- Modify `books_of_time/db/__init__.py`: export `VideoInfoSnapshot`.
- Modify `tests/test_video_stats_worker.py`: assert worker writes metadata snapshot from the same raw payload.
- Modify `books_of_time/collectors/video_stats.py`: decode once, parse and insert both snapshots.
- Modify `docs/TODO.md`: mark the metadata snapshot TODO complete.

---

### Task 1: Video Metadata Parser

**Files:**
- Modify: `books_of_time/parsers/video.py`
- Test: `tests/test_video_stats.py`

**Interfaces:**
- Produces: `ParsedVideoInfoSnapshot` dataclass.
- Produces: `parse_video_info_snapshot(payload: dict[str, Any], *, captured_at: datetime, raw_payload_id: int | None) -> ParsedVideoInfoSnapshot`.

- [ ] **Step 1: Write failing parser tests**

Add tests asserting the parser maps metadata and deduplicates tag names:

```python
from books_of_time.parsers.video import parse_video_info_snapshot


def test_parse_video_info_snapshot_maps_title_owner_and_tags() -> None:
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    payload = {
        "code": 0,
        "data": {
            "bvid": "BV1abc",
            "title": "Demo Video",
            "desc": "A useful description",
            "owner": {"mid": 12345, "name": "Example UP"},
            "tag": [{"tag_name": "攻略"}, {"name": "游戏"}],
            "tname": "单机游戏",
        },
    }

    snapshot = parse_video_info_snapshot(
        payload,
        captured_at=captured_at,
        raw_payload_id=42,
    )

    assert snapshot.bvid == "BV1abc"
    assert snapshot.captured_at == captured_at
    assert snapshot.title == "Demo Video"
    assert snapshot.description == "A useful description"
    assert snapshot.owner_mid == 12345
    assert snapshot.owner_name == "Example UP"
    assert snapshot.tags == {
        "names": ["攻略", "游戏", "单机游戏"],
        "source_fields": ["tag", "tname"],
    }
    assert snapshot.raw_payload_id == 42
```

```python
def test_parse_video_info_snapshot_accepts_missing_optional_metadata() -> None:
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    snapshot = parse_video_info_snapshot(
        {"code": 0, "data": {"bvid": "BV1abc"}},
        captured_at=captured_at,
        raw_payload_id=None,
    )

    assert snapshot.title is None
    assert snapshot.description is None
    assert snapshot.owner_mid is None
    assert snapshot.owner_name is None
    assert snapshot.tags == {"names": [], "source_fields": []}
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_video_stats.py -v
```

Expected: FAIL because `parse_video_info_snapshot` does not exist.

- [ ] **Step 3: Implement parser**

Add the dataclass and parser in `books_of_time/parsers/video.py`. Use helper functions that preserve first-seen tag order and deduplicate names.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_video_stats.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/parsers/video.py tests/test_video_stats.py
git commit -m "feat: parse video info snapshots"
```

---

### Task 2: Video Metadata Persistence

**Files:**
- Modify: `books_of_time/db/models.py`
- Modify: `books_of_time/db/repositories.py`
- Modify: `books_of_time/db/__init__.py`
- Test: `tests/test_video_stats_worker.py`

**Interfaces:**
- Produces: `VideoInfoSnapshot` ORM model.
- Produces: `VideoInfoSnapshotRepository.insert_from_parsed(parsed: ParsedVideoInfoSnapshot) -> VideoInfoSnapshot`.

- [ ] **Step 1: Write failing repository/model coverage**

Extend `tests/test_video_stats_worker.py` imports and add a focused test:

```python
from books_of_time.db.models import VideoInfoSnapshot
from books_of_time.db.repositories import VideoInfoSnapshotRepository
from books_of_time.parsers.video import parse_video_info_snapshot


@pytest.mark.asyncio
async def test_video_info_snapshot_repository_inserts_parsed_snapshot() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    parsed = parse_video_info_snapshot(
        {
            "code": 0,
            "data": {
                "bvid": "BV1abc",
                "title": "Demo Video",
                "owner": {"mid": 12345, "name": "Example UP"},
                "tag": ["攻略"],
            },
        },
        captured_at=captured_at,
        raw_payload_id=42,
    )

    async with session_factory() as session:
        row = await VideoInfoSnapshotRepository(session).insert_from_parsed(parsed)
        await session.commit()

        stored = await session.get(VideoInfoSnapshot, ("BV1abc", captured_at))

    assert row.bvid == "BV1abc"
    assert stored is not None
    assert stored.title == "Demo Video"
    assert stored.owner_mid == 12345
    assert stored.tags == {"names": ["攻略"], "source_fields": ["tag"]}
    assert stored.raw_payload_id == 42
    await engine.dispose()
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_video_stats_worker.py::test_video_info_snapshot_repository_inserts_parsed_snapshot -v
```

Expected: FAIL because `VideoInfoSnapshot` or `VideoInfoSnapshotRepository` does not exist.

- [ ] **Step 3: Implement model, export, and repository**

Add the ORM model with the composite primary key, JSON `tags`, and the bvid/time index. Add the repository insertion method mirroring `VideoMetricSnapshotRepository`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_video_stats_worker.py::test_video_info_snapshot_repository_inserts_parsed_snapshot -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/db/models.py books_of_time/db/repositories.py books_of_time/db/__init__.py tests/test_video_stats_worker.py
git commit -m "feat: persist video info snapshots"
```

---

### Task 3: Collector Integration And TODO

**Files:**
- Modify: `books_of_time/collectors/video_stats.py`
- Modify: `tests/test_video_stats_worker.py`
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: `parse_video_info_snapshot(...)`.
- Consumes: `VideoInfoSnapshotRepository.insert_from_parsed(...)`.
- Produces: successful video stats worker runs that write both metric and info snapshots from one raw payload.

- [ ] **Step 1: Write failing collector assertion**

Extend `FakeBilibiliClient` payload in `tests/test_video_stats_worker.py` with title, description, owner, and tags. In `test_worker_fetch_video_stats_archives_raw_then_writes_snapshot`, select `VideoInfoSnapshot` and assert it exists with the same `raw_payload_id`.

```python
info_snapshot = await session.scalar(select(VideoInfoSnapshot))

assert info_snapshot is not None
assert info_snapshot.bvid == "BV1abc"
assert info_snapshot.title == "Demo Video"
assert info_snapshot.description == "A useful description"
assert info_snapshot.owner_mid == 12345
assert info_snapshot.owner_name == "Example UP"
assert info_snapshot.tags["names"] == ["攻略", "游戏"]
assert info_snapshot.raw_payload_id == raw.id
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_video_stats_worker.py::test_worker_fetch_video_stats_archives_raw_then_writes_snapshot -v
```

Expected: FAIL because the collector only writes metric snapshots.

- [ ] **Step 3: Implement collector integration**

Decode `result.body` once into `payload`, parse stats and metadata from that payload, insert both snapshots, and keep the existing coverage draft unchanged.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_video_stats_worker.py -v
```

Expected: PASS.

- [ ] **Step 5: Update TODO and run full verification**

Mark `保存视频标题、简介、tag、UP 主信息快照。` complete in `docs/TODO.md`.

Run:

```bash
uv run pytest
uv run ruff check .
```

Expected: all tests pass and Ruff reports no issues.

- [ ] **Step 6: Commit**

```bash
git add books_of_time/collectors/video_stats.py tests/test_video_stats_worker.py docs/TODO.md
git commit -m "feat: collect video info snapshots"
```

---

## Self-Review

- Spec coverage: parser, schema, repository, collector insertion, TODO update, and full verification are covered.
- Placeholder scan: no `TBD`, vague implementation steps, or missing commands.
- Type consistency: plan uses `ParsedVideoInfoSnapshot`, `parse_video_info_snapshot`, `VideoInfoSnapshot`, and `VideoInfoSnapshotRepository` consistently across tasks.
