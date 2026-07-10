# Event Archive Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement event metadata, targets, video associations, versioned keywords, and the first event management CLI commands.

**Architecture:** `EventRepository` owns normalization and transactional invariants across four ORM tables. CLI remains thin; seed BVID target creation attaches the video and enqueues one idempotent stats task in the same transaction.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, Alembic, argparse, pytest-asyncio, Ruff.

## Constraints

- Store public UID/BVID/keyword values in clear text for verification.
- Do not auto-associate keyword matches.
- Numeric event ID and slug both resolve through one repository method.
- Add a new Alembic revision; never edit `0001_initial` after this feature begins.
- Follow TDD and commit each task separately.

### Task 1: Event ORM And Normalization

- [x] Write model tests for constraints, relationships by IDs, target normalization, and duplicate stable keys.
- [x] Verify RED for missing event models/helpers.
- [x] Add `Event`, `EventTarget`, `EventVideo`, and `EventKeyword` plus indexes and uniqueness constraints.
- [x] Add `normalize_event_slug`, `normalize_event_target`, and validation for event windows, UID, BVID, keyword, and game values.
- [x] Verify focused tests and commit `feat: add event archive data model`.

### Task 2: Event Repository Invariants

- [x] Write tests for create/resolve/list, idempotent target add, keyword synchronization, seed attachment/task enqueue, manual attachment, and video listing.
- [x] Verify RED for missing repository.
- [x] Implement `EventRepository` methods with explicit `LookupError`/`ValueError` failures and no partial writes.
- [x] Verify repository plus task queue tests and commit `feat: manage event archive records`.

### Task 3: Event Management CLI And Migration

- [x] Write parser/dispatch and SQLite integration tests for `event create`, `event list`, `event add-target`, and `event list-videos`.
- [x] Verify RED for missing commands.
- [x] Implement CLI handlers with ISO timestamp parsing, stable IDs, target reasons, and bounded list limits.
- [x] Generate and review a static `0002_event_archive` revision; run SQLite and isolated PostgreSQL migration cycles.
- [x] Update TODO core Event Archive items and run full verification.
- [x] Commit `feat: add event archive management CLI`.

## Self-review

- All Event Core acceptance points are covered.
- Scheduler discovery, coverage summary, and timeline export are explicitly deferred to the next plan.
