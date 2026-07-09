# Phase 1K View Growth Policy Implementation Plan

> **Execution mode:** Implement inline in this main session. Avoid opening subagents unless the user explicitly asks for them again. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute one-hour view growth from stored metric snapshots and use it to return a dynamic next snapshot time for one BV.

**Architecture:** Add a repository method for view growth, then add a small async policy helper that calls the existing `snapshot_policy.get_next_snapshot_at()`.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, pytest-asyncio, Ruff.

## Global Constraints

- Do not enqueue scheduler tasks in this slice.
- Reuse `get_next_snapshot_at()`; do not duplicate interval thresholds.
- Use `VideoMetricSnapshot.view_count` only.
- Return `None` for growth when no usable snapshots exist.
- Clamp negative growth to `0`.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and `books_of_time/http/rate_limiter.py`.
- Execute inline in this main session; do not dispatch subagents unless the user asks again.

---

## File Structure

- Modify `books_of_time/db/repositories.py`: add `VideoMetricSnapshotRepository.get_view_growth_since()`.
- Create `books_of_time/task_orchestrator/video_snapshot_policy.py`: add `get_next_video_snapshot_at()`.
- Create `tests/test_video_snapshot_policy_db.py`: repository and service tests.
- Modify `docs/TODO.md`: mark the view-growth policy TODO complete.

---

### Task 1: Repository View Growth

**Files:**
- Modify: `books_of_time/db/repositories.py`
- Test: `tests/test_video_snapshot_policy_db.py`

**Interfaces:**
- Produces: `VideoMetricSnapshotRepository.get_view_growth_since(bvid: str, since: datetime, now: datetime) -> int | None`.

- [ ] **Step 1: Write failing repository tests**

Add tests for baseline-before-cutoff, oldest-in-window fallback, and negative-growth clamping.

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/test_video_snapshot_policy_db.py -v`.

Expected: FAIL because `get_view_growth_since()` does not exist.

- [ ] **Step 3: Implement repository method**

Use SQLAlchemy selects for latest usable snapshot, baseline before cutoff, and oldest in-window fallback.

- [ ] **Step 4: Verify GREEN**

Run `uv run pytest tests/test_video_snapshot_policy_db.py -v`.

Expected: PASS for repository tests.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/db/repositories.py tests/test_video_snapshot_policy_db.py
git commit -m "feat: compute video view growth"
```

---

### Task 2: BV-Level Next Snapshot Helper

**Files:**
- Create: `books_of_time/task_orchestrator/video_snapshot_policy.py`
- Modify: `tests/test_video_snapshot_policy_db.py`
- Modify: `docs/TODO.md`

**Interfaces:**
- Produces: `get_next_video_snapshot_at(session, *, bvid, published_at, now, core_window=None) -> datetime | None`.

- [ ] **Step 1: Write failing service test**

Insert metric snapshots with growth above the high threshold and assert the helper returns the next 5-minute slot after the six-hour age boundary.

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/test_video_snapshot_policy_db.py -v`.

Expected: FAIL because the helper module does not exist.

- [ ] **Step 3: Implement helper**

Compute `since = now - timedelta(hours=1)`, call the repository method, and pass the result to `get_next_snapshot_at()`.

- [ ] **Step 4: Verify GREEN and full verification**

Run:

```bash
uv run pytest tests/test_video_snapshot_policy_db.py -v
uv run pytest
uv run ruff check .
```

Expected: all tests pass and Ruff reports no issues.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/task_orchestrator/video_snapshot_policy.py tests/test_video_snapshot_policy_db.py docs/TODO.md
git commit -m "feat: add video snapshot policy helper"
```

---

## Self-Review

- Spec coverage: repository growth, service helper, TODO update, and full verification are covered.
- Placeholder scan: no `TBD` or unspecified commands.
- Type consistency: method and helper names match the spec.
