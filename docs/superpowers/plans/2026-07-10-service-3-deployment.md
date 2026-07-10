# Service-3 Deployment Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the long-running service reproducibly deployable with an external PostgreSQL database through committed Alembic migrations, an application-only Docker image/Compose file, and a Linux systemd unit while preserving Windows development.

**Architecture:** The application never creates or owns PostgreSQL during normal startup. Deployments explicitly run `alembic upgrade head`, `service doctor` verifies the committed schema revision, and Docker/systemd launch the same `python main.py service run` kernel with local raw/media directories.

**Tech Stack:** Python 3.12, Alembic, PostgreSQL, uv 0.8.15 image, Docker Compose, systemd, pytest, Ruff.

## Global Constraints

- No PostgreSQL container in Compose.
- No automatic migration from `service run`.
- Media and raw payload files use local mounted filesystems.
- Docker image runs as non-root.
- Linux and Docker read secrets from environment files; examples contain no credentials.
- Windows keeps `uv run python main.py service run`.
- Commit static Alembic revision files; do not import live ORM metadata from migration `upgrade()`.

### Task 1: Committed Initial Migration And Schema Gate

- [ ] Add tests for `get_expected_schema_revision()`, current revision lookup, doctor success on matching revision, and doctor failure for missing/outdated `alembic_version`.
- [ ] Run focused tests and verify RED.
- [ ] Stop ignoring `alembic/versions/*.py`; update `alembic/env.py` to use `load_config()` so `BOT_DATABASE_URL` and `BOT_CONFIG` work for deployment commands.
- [ ] Generate a static initial revision against an empty temporary database, review every table/index/enum, and ensure upgrade/downgrade contain no runtime metadata calls.
- [ ] Add schema revision checking to `ServiceHealthChecker.doctor`; database reachability/schema-table errors remain separate from revision mismatch details.
- [ ] Update SQLite service tests to stamp the expected revision explicitly.
- [ ] Run migration upgrade/downgrade/upgrade against temporary SQLite and focused service tests.
- [ ] Commit with `feat: add reproducible database migration`.

### Task 2: Docker Application Deployment

- [ ] Add artifact tests that parse Compose YAML and assert exactly one application service, no PostgreSQL image/service, external database env wiring, local raw/media mounts, host-gateway mapping, healthcheck, and graceful stop period.
- [ ] Verify RED because Docker artifacts are absent.
- [ ] Add `.dockerignore`, pinned `Dockerfile`, `compose.yaml`, and `.env.example` using `ghcr.io/astral-sh/uv:0.8.15-python3.12-bookworm-slim`, `uv sync --frozen --no-dev --no-install-project`, non-root execution, and direct venv Python command.
- [ ] Add container healthcheck invoking `service health`; do not expose a port.
- [ ] Parse artifacts and run `docker compose config` / `docker build` when Docker is available; record an explicit limitation otherwise.
- [ ] Commit with `build: add external-database Docker deployment`.

### Task 3: Linux Native Deployment And Operations Guide

- [ ] Add artifact tests for systemd `User`, `WorkingDirectory`, `EnvironmentFile`, `ExecStartPre=service doctor`, `ExecStart=service run`, restart policy, state directory, and stop timeout.
- [ ] Verify RED because deployment files are absent.
- [ ] Add `deploy/systemd/books-of-time.service`, `deploy/books-of-time.env.example`, and `docs/DEPLOYMENT.md` covering external PostgreSQL reachability, Docker host-gateway, explicit migration, existing unversioned database stamping, local permissions, systemd installation, Windows development, upgrade, rollback, backup, and health/status commands.
- [ ] Update README links and TODO verified states.
- [ ] Run full pytest, Ruff, format, YAML parsing, Compose validation if available, and Git whitespace checks.
- [ ] Commit with `docs: add native service deployment guide`.

## Plan Self-review

- Migration, Docker, Linux native, Windows continuity, and external PostgreSQL are covered.
- Schema mutation remains an explicit operator action.
- Runtime media remains local and no object store is introduced.
- Event Archive remains the next implementation plan after this deployment gate.
