# Phase 1G Discovery Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a testable discovery loop that scans configured matrix UIDs and enqueues fresh video stats tasks through the existing scheduler.

**Architecture:** Create a focused `DiscoveryLoop` service that depends on a Bilibili-like client, the existing session factory, and configured UID list. Keep CLI as a thin wrapper that builds the loop from config and exposes finite smoke-run options.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, argparse CLI, pytest-asyncio, Ruff.

## Global Constraints

- Only scan configured matrix UIDs in this slice.
- Event-level UID pools and game-level UID pools remain out of scope.
- Scan page 1 only, ordered by publish time.
- Do not add Redis, background service management, or a new scheduler daemon.
- Keep the loop testable without real sleep or network.
- Do not bypass platform rate limits; all requests continue through `BilibiliPlatformClient`.
- One UID failure should not prevent other UIDs in the same round from being scanned.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and `books_of_time/http/rate_limiter.py`.
- Execute inline in this main session; do not dispatch subagents unless the user asks again.

---

## File Structure

- Create `books_of_time/task_orchestrator/discovery_loop.py`: loop service, result dataclass, client protocol.
- Modify `books_of_time/cli.py`: add `bot discovery loop` and helper `_run_discovery_loop`.
- Modify `tests/test_discovery_loop.py`: loop service tests.
- Modify `tests/test_cli.py`: parser and helper tests.
- Modify `docs/TODO.md`: mark the configured matrix UID discovery loop complete.

---

### Task 1: Discovery Loop Service

**Files:**
- Create: `books_of_time/task_orchestrator/discovery_loop.py`
- Test: `tests/test_discovery_loop.py`

**Interfaces:**
- Produces: `DiscoveryLoopResult(uids_scanned: int, videos_seen: int, videos_created: int, errors: int)`.
- Produces: `DiscoveryLoop.run_once(now: datetime | None = None) -> DiscoveryLoopResult`.
- Produces: `DiscoveryLoop.run_loop(interval_seconds: float, max_iterations: int | None = None, stop_when_idle: bool = False, sleep: Callable[[float], Awaitable[None] | None] | None = None) -> DiscoveryLoopResult`.

- [ ] **Step 1: Write failing tests**

Add tests with a fake client returning `FetchResult` JSON bodies:

```python
async def test_discovery_loop_scans_configured_uids_and_enqueues_tasks() -> None:
    result = await loop.run_once(now=now)

    assert result.uids_scanned == 2
    assert result.videos_seen == 2
    assert result.videos_created == 2
```

```python
async def test_discovery_loop_continues_after_uid_failure() -> None:
    result = await loop.run_once(now=now)

    assert result.uids_scanned == 1
    assert result.errors == 1
```

```python
async def test_discovery_loop_run_loop_uses_injected_sleep() -> None:
    result = await loop.run_loop(interval_seconds=0.5, max_iterations=2, sleep=fake_sleep)

    assert slept == [0.5, 0.5]
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_discovery_loop.py -v
```

Expected: FAIL because `books_of_time.task_orchestrator.discovery_loop` does not exist.

- [ ] **Step 3: Implement loop service**

Implement `DiscoveryLoop` using `parse_user_video_list()` and `DiscoveryScheduler.handle_discovered_videos()`. Log and continue after per-UID errors.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_discovery_loop.py tests/test_discovery_scheduler.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/task_orchestrator/discovery_loop.py tests/test_discovery_loop.py
git commit -m "feat: add discovery loop service"
```

---

### Task 2: Discovery Loop CLI

**Files:**
- Modify: `books_of_time/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `DiscoveryLoop.run_loop(...)`.
- Produces: CLI command `bot discovery loop`.
- Produces: helper `_run_discovery_loop(cfg: dict, interval_seconds: float | None, max_iterations: int | None, stop_when_idle: bool) -> None`.

- [ ] **Step 1: Write failing CLI tests**

Add parser test:

```python
def test_discovery_loop_parser_accepts_options() -> None:
    args = build_parser().parse_args([
        "discovery",
        "loop",
        "--interval-seconds",
        "0.1",
        "--max-iterations",
        "1",
        "--stop-when-idle",
    ])

    assert args.command == "discovery"
    assert args.discovery_command == "loop"
```

Add helper test that monkeypatches `cli.build_bilibili_client` to a fake client,
uses `cfg["discovery"]["matrix_uids"]`, and verifies a task is created.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_cli.py -v
```

Expected: FAIL because command/helper is missing.

- [ ] **Step 3: Implement CLI**

Add parser branch and helper. Normalize `matrix_uids` to strings. Use configured
`scheduler.discovery_scan_seconds` when `--interval-seconds` is omitted.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_cli.py tests/test_discovery_loop.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add books_of_time/cli.py tests/test_cli.py
git commit -m "feat: add discovery loop cli"
```

---

### Task 3: TODO Sync And Full Verification

**Files:**
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: completed discovery loop service and CLI.

- [ ] **Step 1: Update TODO**

Mark `实现常驻 discovery loop，每分钟扫描配置的矩阵 UID。` complete.

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
git commit -m "docs: mark discovery loop progress"
```

---

## Self-Review

- Spec coverage: loop service, CLI, config keys, failure continuation, TODO sync, and verification are covered.
- Placeholder scan: no TBD/fill-later placeholders are present.
- Type consistency: names match the design document.
