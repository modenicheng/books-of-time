# Phase 1L Snapshot Scheduler Implementation Plan

> **Execution mode:** Implement inline in this main session. Avoid opening subagents unless the user explicitly asks for them again. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enqueue follow-up `FETCH_VIDEO_STATS` tasks from the dynamic snapshot policy after visible stats collections.

**Architecture:** Add a `VideoSnapshotScheduler` service that loads `KnownVideo`, computes next time via `get_next_video_snapshot_at()`, and enqueues an idempotent task. Wire it optionally into `VideoStatsCollector`.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, pytest-asyncio, Ruff.

## Global Constraints

- Schedule only videos present in `known_videos`; manual one-off `monitor-video` tasks without known `pubdate` are not auto-looped in this slice.
- Schedule only visible collections. Deleted, invisible, and permission-denied payloads should not enqueue another stats task.
- Use `get_next_video_snapshot_at()` for timing.
- Use an idempotency key containing BV and next timestamp.
- Do not add new Bilibili API requests.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and `books_of_time/http/rate_limiter.py`.

---

## File Structure

- Create `books_of_time/task_orchestrator/video_snapshot_scheduler.py`: scheduler service.
- Modify `books_of_time/collectors/video_stats.py`: optional scheduler integration.
- Modify `books_of_time/app.py`: pass scheduler into the production collector.
- Modify `tests/test_video_stats_worker.py`: worker integration assertions.
- Create `tests/test_video_snapshot_scheduler.py`: direct scheduler tests.
- Modify `docs/TODO.md`: mark scheduler integration complete.

---

### Task 1: Video Snapshot Scheduler

**Files:**
- Create: `books_of_time/task_orchestrator/video_snapshot_scheduler.py`
- Test: `tests/test_video_snapshot_scheduler.py`

**Interfaces:**
- Produces: `VideoSnapshotScheduler.schedule_next_for_video(session, bvid, now) -> CollectionTask | None`.

- [ ] **Step 1: Write failing scheduler tests**

Test known video enqueue and unknown video no-op.

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/test_video_snapshot_scheduler.py -v`.

Expected: FAIL because scheduler module does not exist.

- [ ] **Step 3: Implement scheduler service**

Load `KnownVideo`; compute next time; enqueue with priority 80, reason `snapshot_policy`, and idempotency key containing the ISO timestamp.

- [ ] **Step 4: Verify GREEN**

Run the same test file.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/task_orchestrator/video_snapshot_scheduler.py tests/test_video_snapshot_scheduler.py
git commit -m "feat: add video snapshot scheduler"
```

---

### Task 2: Collector And App Integration

**Files:**
- Modify: `books_of_time/collectors/video_stats.py`
- Modify: `books_of_time/app.py`
- Modify: `tests/test_video_stats_worker.py`
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: `VideoSnapshotScheduler.schedule_next_for_video(...)`.
- Produces: visible worker runs that enqueue a next stats task; unavailable runs do not.

- [ ] **Step 1: Write failing worker tests**

Assert visible known video creates a pending follow-up task, and deleted known video does not.

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/test_video_stats_worker.py -v`.

Expected: FAIL because the collector does not call the scheduler.

- [ ] **Step 3: Implement collector and app wiring**

Add optional scheduler dependency to `VideoStatsCollector`; call it only after visible metric/info inserts. In `app.py`, instantiate `VideoSnapshotScheduler()`.

- [ ] **Step 4: Verify GREEN and full verification**

Run:

```bash
uv run pytest tests/test_video_stats_worker.py tests/test_video_snapshot_scheduler.py -v
uv run pytest
uv run ruff check .
```

Expected: all tests pass and Ruff reports no issues.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/collectors/video_stats.py books_of_time/app.py tests/test_video_stats_worker.py docs/TODO.md
git commit -m "feat: schedule next video snapshots"
```

---

## Self-Review

- Spec coverage: scheduler service, collector integration, app wiring, TODO update, and full verification are covered.
- Placeholder scan: no `TBD` or unspecified commands.
- Type consistency: scheduler method names match the spec.
