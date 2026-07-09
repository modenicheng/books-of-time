# Phase 1H Video Stats Query Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `bot video stats BVxxxx` CLI that lists recent collected video metric snapshots.

**Architecture:** Add a read method to `VideoMetricSnapshotRepository`, then expose it through the existing `video` CLI command group with log-based output.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, argparse CLI, pytest-asyncio, Ruff.

## Global Constraints

- Read-only; no new collection tasks are created.
- Do not change `video_metric_snapshots` schema in this slice.
- Do not infer missing metrics or compute trend deltas in this slice.
- Keep CLI output log-based, matching existing `coverage` and `task list` commands.
- Default limit is `20`; clamp to `1..200`.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and `books_of_time/http/rate_limiter.py`.
- Execute inline in this main session; do not dispatch subagents unless the user asks again.

---

## File Structure

- Modify `books_of_time/db/repositories.py`: add `VideoMetricSnapshotRepository.list_for_bvid()`.
- Modify `books_of_time/cli.py`: add `bot video stats` parser branch and `_show_video_stats()`.
- Modify `tests/test_video_stats_worker.py` or create focused repository test in `tests/test_video_stats_query.py`.
- Modify `tests/test_cli.py`: parser and helper tests.
- Modify `docs/TODO.md`: mark the video stats query CLI item complete.

---

### Task 1: Video Stats Repository Query

**Files:**
- Modify: `books_of_time/db/repositories.py`
- Test: `tests/test_video_stats_query.py`

**Interfaces:**
- Produces: `VideoMetricSnapshotRepository.list_for_bvid(bvid: str, limit: int = 20) -> list[VideoMetricSnapshot]`.

- [ ] **Step 1: Write failing tests**

Add a test that inserts two snapshots and expects newest-first ordering:

```python
async def test_video_metric_repository_lists_snapshots_newest_first() -> None:
    rows = await repo.list_for_bvid(bvid="BV1abc", limit=1)

    assert [row.captured_at for row in rows] == [newer_time]
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_video_stats_query.py -v
```

Expected: FAIL because `list_for_bvid()` does not exist.

- [ ] **Step 3: Implement repository method**

Use `select(VideoMetricSnapshot).where(...).order_by(VideoMetricSnapshot.captured_at.desc()).limit(limit)`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_video_stats_query.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/db/repositories.py tests/test_video_stats_query.py
git commit -m "feat: add video stats query repository"
```

---

### Task 2: Video Stats CLI

**Files:**
- Modify: `books_of_time/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `VideoMetricSnapshotRepository.list_for_bvid(...)`.
- Produces: `bot video stats BVxxxx --limit N`.
- Produces: `_show_video_stats(cfg: dict, bvid: str, limit: int) -> None`.

- [ ] **Step 1: Write failing CLI tests**

Add parser and helper tests:

```python
def test_video_stats_parser_accepts_bvid_and_limit() -> None:
    args = build_parser().parse_args(["video", "stats", "BV1abc", "--limit", "5"])

    assert args.video_command == "stats"
    assert args.bvid == "BV1abc"
    assert args.limit == 5
```

```python
async def test_show_video_stats_logs_latest_snapshots(tmp_path, caplog) -> None:
    await cli._show_video_stats(cfg, "BV1abc", limit=20)

    assert "BV1abc" in caplog.text
    assert "view=100" in caplog.text
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_cli.py -v
```

Expected: FAIL because parser/helper is missing.

- [ ] **Step 3: Implement CLI**

Add `video stats` parser and `_show_video_stats()`. Clamp `limit` to `1..200`. Log empty state when no rows exist.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_cli.py tests/test_video_stats_query.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/cli.py tests/test_cli.py
git commit -m "feat: add video stats query cli"
```

---

### Task 3: TODO Sync And Full Verification

**Files:**
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: completed repository and CLI behavior.

- [ ] **Step 1: Update TODO**

Mark `增加 bot video stats BVxxxx 查询 CLI。` complete.

- [ ] **Step 2: Full verification**

Run:

```bash
uv run pytest
uv run ruff check .
```

Expected: all tests pass and Ruff reports no issues.

- [ ] **Step 3: Commit**

```bash
git add docs/TODO.md
git commit -m "docs: mark video stats query progress"
```

---

## Self-Review

- Spec coverage: repository query, CLI parser/helper, empty state, TODO sync, and verification are covered.
- Placeholder scan: no TBD/fill-later placeholders are present.
- Type consistency: repository and helper names match the design document.
