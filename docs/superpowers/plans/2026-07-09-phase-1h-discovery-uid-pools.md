# Phase 1H Discovery UID Pools Implementation Plan

> **Execution mode:** Implement inline in this main session. Avoid opening subagents unless the user explicitly asks for them again.

**Goal:** Support global, game-level, and event-level UID pools for discovery loop scanning.

**Architecture:** Add `DiscoveryUidSource` and optional source pool fields to discovered videos, teach the CLI to resolve UID sources from config, and persist pool metadata in `fetch_video_stats` task payloads.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, argparse CLI, pytest-asyncio, Ruff.

## Global Constraints

- Do not add event archive tables in this slice.
- Preserve the existing `matrix_uids` config key.
- Support both `pool: [uids]` and `pool: {uids: [...]}`.
- Continue scanning page 1 only.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and `books_of_time/http/rate_limiter.py`.

---

### Task 1: Source Metadata In Discovery Loop

- [ ] Add failing tests in `tests/test_discovery_loop.py` for `DiscoveryUidSource` scanning and task payload metadata.
- [ ] Add `DiscoveryUidSource` to `books_of_time/task_orchestrator/discovery_loop.py`.
- [ ] Extend `DiscoveredVideo` and `parse_user_video_list()` with optional pool metadata.
- [ ] Write source metadata into `CollectionTask.payload`.
- [ ] Verify with `uv run pytest tests/test_discovery_loop.py tests/test_discovery_scheduler.py -v`.
- [ ] Commit as `feat: track discovery uid source metadata`.

### Task 2: Config Resolution And TODO

- [ ] Add failing CLI test for `game_uid_pools` and `event_uid_pools`.
- [ ] Add `_resolve_discovery_uid_sources(discovery_cfg: dict)`.
- [ ] Use resolved sources in `_run_discovery_loop()`.
- [ ] Update `config/config.yaml.example`.
- [ ] Mark TODO item complete.
- [ ] Verify with `uv run pytest` and `uv run ruff check .`.
- [ ] Commit as `feat: support discovery uid pools`.
