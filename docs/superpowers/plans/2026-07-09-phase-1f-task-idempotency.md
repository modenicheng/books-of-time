# Phase 1F Task Idempotency Implementation Plan

> **Execution mode:** Implement inline in this main session. Avoid opening subagents unless the user explicitly asks for them again.

**Goal:** Prevent duplicate active queue tasks when callers provide a stable idempotency key.

**Architecture:** Add nullable `idempotency_key` to `collection_tasks`, create a partial unique index for active task states, and teach `CollectionTaskRepository.enqueue()` to return the existing active task for the same key. Wire CLI/manual discovery enqueue sites to provide conservative keys where duplicate active tasks are undesirable.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, argparse CLI, pytest-asyncio, Ruff.

## Global Constraints

- Only active tasks are deduplicated: `pending`, `running`, and `backoff`.
- Completed states remain reusable: `succeeded` and `failed` do not block future enqueue.
- Callers may omit `idempotency_key`; those tasks keep the existing behavior.
- Do not add a daemon, Redis, or migration framework in this slice.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and `books_of_time/http/rate_limiter.py`.

---

## File Structure

- Modify `books_of_time/db/models.py`: add `CollectionTask.idempotency_key` and a partial unique index.
- Modify `books_of_time/db/repositories.py`: add optional `idempotency_key` to `enqueue()`.
- Modify `books_of_time/cli.py`: pass keys for manual enqueue commands.
- Modify `books_of_time/task_orchestrator/discovery.py`: pass a key for fresh discovery stat tasks.
- Modify `tests/test_task_queue.py`: repository idempotency tests.
- Modify `tests/test_cli.py`: duplicate CLI enqueue helper test if needed.
- Modify `docs/TODO.md`: mark the task uniqueness/idempotency item complete.

---

### Task 1: Repository Idempotency

- [ ] Write failing tests showing two active enqueue calls with the same key return one task, while a succeeded task with the same key does not block a new task.
- [ ] Add model column and active partial unique index.
- [ ] Add `idempotency_key: str | None = None` to `CollectionTaskRepository.enqueue()`.
- [ ] Query for existing active tasks by key before inserting.
- [ ] Run `uv run pytest tests/test_task_queue.py -v`.
- [ ] Run `uv run ruff check books_of_time/db/models.py books_of_time/db/repositories.py tests/test_task_queue.py`.
- [ ] Commit as `feat: add task idempotency keys`.

### Task 2: Enqueue Callers And Docs

- [ ] Pass idempotency keys in `monitor-video`, `video comments --mode hot`, `collect-latest-comments`, and fresh discovery video stat enqueue.
- [ ] Add or update CLI tests to prove duplicate active CLI enqueue returns a single task.
- [ ] Mark the TODO idempotency item complete.
- [ ] Run `uv run pytest`.
- [ ] Run `uv run ruff check .`.
- [ ] Commit as `feat: deduplicate manual task enqueue`.
