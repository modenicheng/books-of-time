# Collection Evidence Foundations C1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Begin preserving the irreversible comment, discovery-source, and HTTP-attempt evidence required by collection-first snapshot cohorts.

**Architecture:** Extend the existing SQLAlchemy/Alembic schema without replacing current tables, then populate the new fields through the existing parser, repository, discovery, worker, and unified HTTP boundaries. HTTP evidence uses a context-local sink so login and other non-collection calls remain independent; successful responses continue through collector-owned raw persistence, while classified failed responses are archived before `RequestFailure` propagates.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async ORM, Alembic, SQLite test fixtures, PostgreSQL production constraints, curl-cffi, bilibili-api-python, zstandard raw storage, pytest, Ruff.

## Global Constraints

- Preserve public Bilibili author identifiers and stable public metadata for manual verification; do not anonymize them.
- Never persist Cookie, CSRF, refresh token, authenticated headers, request body, or a plaintext authenticated URL in HTTP-attempt evidence.
- Media remains on the local filesystem. Raw payload storage continues to support filesystem and MinIO.
- Every formal collection request continues through the unified HTTP/rate-limit path.
- Comment and raw history is append-only. Existing content and raw references are never overwritten.
- `platform_created_at`, request timestamps, and database timestamps are UTC-aware.
- Existing nullable rows remain valid after migration; reparse fills only missing entity evidence.
- C1 adds evidence capture only. Cohort scheduling, scan runs, visibility reconciliation, and full worker short-transaction/CAS work belong to C2-C7.
- Use test-first red/green cycles and one Conventional Commit per task.

## File Map

- `books_of_time/db/models.py`: add comment evidence columns, `KnownVideoSource`, and `HttpRequestAttempt`.
- `books_of_time/db/repositories.py`: persist missing comment evidence, upsert video sources, and manage attempt lifecycle.
- `books_of_time/parsers/comments.py`: parse `ctime` plus an allowlisted stable public member snapshot.
- `books_of_time/task_orchestrator/discovery_loop.py`: carry configured source metadata.
- `books_of_time/task_orchestrator/discovery_sources.py`: resolve `game_id`, `official`, and `monitored` defaults.
- `books_of_time/task_orchestrator/discovery.py`: persist every source association observed for a BVID.
- `books_of_time/service/scheduled_jobs.py`: merge duplicate-MID source associations into one request payload.
- `books_of_time/collectors/user_videos.py`: preserve source association and raw-page provenance.
- `books_of_time/http/client.py`: expose request timing/attempt identity and report all response/transport outcomes.
- `books_of_time/http/evidence.py`: define the context-local evidence sink protocol.
- `books_of_time/db/http_evidence.py`: archive failed response bodies and write attempt rows through the current worker session.
- `books_of_time/worker.py`: activate evidence capture only around collection-task execution.
- `books_of_time/app.py`: provide the worker's raw store to the evidence recorder.
- `alembic/versions/0008_collection_evidence_foundations.py`: static upgrade/downgrade revision.
- `config/config.yaml.example`, `docs/CONFIGURATION.md`: make game source metadata explicit.
- `docs/COLLECTION.md`, `docs/DATA_MODEL.md`: document evidence semantics and limits.
- `tests/test_evidence_models.py`: ORM and repository contracts.
- `tests/test_comments_parser.py`, `tests/test_comment_repositories.py`: parser/persistence behavior.
- `tests/test_discovery_scheduler.py`, `tests/test_service_scheduled_handlers.py`, `tests/test_user_videos_worker.py`, `tests/test_config_loader.py`: source merging and provenance.
- `tests/test_http_request_attempts.py`, `tests/test_request_errors.py`: attempt lifecycle and failed-body archival.
- `tests/test_schema_migrations.py`: revision head/static migration checks.

---

### Task 1: Add The Static Evidence Schema

**Files:**
- Create: `tests/test_evidence_models.py`
- Create: `alembic/versions/0008_collection_evidence_foundations.py`
- Modify: `books_of_time/db/models.py`
- Modify: `tests/test_schema_migrations.py`

**Interfaces:**
- Produces: `KnownVideoSource`, `HttpRequestAttempt`, and nullable comment evidence columns.
- Produces: Alembic head `0008_collection_evidence_foundations`.
- Consumes: `UTCDateTime`, `json_dict_type`, `bigint_pk_type`, existing `KnownVideo`, `CommentEntity`, `CommentObservation`, `CollectionTask`, and `RawPayload`.

- [ ] **Step 1: Write failing metadata and round-trip tests**

Create `tests/test_evidence_models.py` with an in-memory schema test that imports the two new models, inserts a `KnownVideo`, one `KnownVideoSource`, one `CollectionTask`, and one `HttpRequestAttempt`, then verifies:

```python
assert source.game_id == "genshin_impact"
assert source.official is True
assert source.monitored is True
assert attempt.status == "started"
assert attempt.raw_payload_id is None
assert CommentEntity.__table__.c.platform_created_at.nullable is True
assert CommentObservation.__table__.c.author_public_metadata_extra.nullable is False
```

Add a second test that inserting the same `(bvid, source_mid, pool_type, pool_id)` twice raises `IntegrityError`.

- [ ] **Step 2: Run tests to verify RED**

Run:

```powershell
uv run pytest tests/test_evidence_models.py -q
```

Expected: collection fails because `KnownVideoSource` and `HttpRequestAttempt` do not exist.

- [ ] **Step 3: Add ORM columns and models**

Add these nullable/public fields to both comment tables, using the same names on entity and observation. The entity values mean first-known values and are only backfilled when NULL:

```python
platform_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
author_level: Mapped[int | None] = mapped_column(Integer)
author_official_type: Mapped[int | None] = mapped_column(Integer)
author_official_description: Mapped[str | None] = mapped_column(Text)
author_vip_status: Mapped[int | None] = mapped_column(Integer)
author_vip_type: Mapped[int | None] = mapped_column(Integer)
author_is_senior_member: Mapped[bool | None] = mapped_column(Boolean)
author_public_metadata_extra: Mapped[dict[str, Any]] = mapped_column(
    json_dict_type, nullable=False, default=dict
)
```

Add `KnownVideoSource` after `KnownVideo` with a surrogate bigint primary key, non-null `pool_id`, source/raw timestamps, and:

```python
UniqueConstraint(
    "bvid", "source_mid", "pool_type", "pool_id",
    name="uq_known_video_sources_identity",
)
```

Add indexes on `(bvid, active)`, `(game_id, official, monitored)`, and `source_mid`.

Add `HttpRequestAttempt` near request backoff/budget models with:

```text
id
collection_task_id NULLABLE
snapshot_cohort_id NULLABLE
snapshot_cohort_component_id NULLABLE
status
request_type
attempt_started_at
request_started_at NULLABLE
request_finished_at NULLABLE
response_received_at NULLABLE
duration_ms NULLABLE
method
url_hash
params_hash NULLABLE
http_status NULLABLE
error_type NULLABLE
error_message NULLABLE
raw_payload_id NULLABLE
created_at
```

Use status strings `started`, `succeeded`, `failed`, and `abandoned`; add indexes on `(status, attempt_started_at)`, `collection_task_id`, and `raw_payload_id`.

- [ ] **Step 4: Add the static Alembic revision**

Create revision `0008_collection_evidence_foundations` with `down_revision = "0007_operational_alert_states"`. The upgrade must add the comment columns, create `known_video_sources` and `http_request_attempts`, then create the exact indexes above. The downgrade reverses indexes/tables first and comment columns last. Do not import `Base.metadata` or use autogenerate at runtime.

Update the expected revision assertion and add a static-source test:

```python
assert get_expected_schema_revision() == "0008_collection_evidence_foundations"
assert 'down_revision: str | Sequence[str] | None = "0007_operational_alert_states"' in source
assert 'op.create_table(\n        "known_video_sources"' in source
assert 'op.create_table(\n        "http_request_attempts"' in source
assert "Base.metadata" not in source
```

Add `test_collection_evidence_revision_round_trip` using a temporary SQLite
database and Alembic config. It upgrades to head, downgrades exactly to
`0007_operational_alert_states`, asserts both new tables and columns disappear,
then upgrades to head again and asserts they return. Never point this test at
the operator's configured database.

- [ ] **Step 5: Verify schema GREEN**

Run:

```powershell
uv run pytest tests/test_evidence_models.py tests/test_schema_migrations.py -q
uv run ruff check books_of_time/db/models.py alembic/versions/0008_collection_evidence_foundations.py tests/test_evidence_models.py tests/test_schema_migrations.py
```

Expected: all selected tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 6: Commit**

```powershell
git add books_of_time/db/models.py alembic/versions/0008_collection_evidence_foundations.py tests/test_evidence_models.py tests/test_schema_migrations.py
git commit -m "feat(db): add collection evidence foundations"
```

---

### Task 2: Parse And Persist Platform Comment Evidence

**Files:**
- Modify: `books_of_time/parsers/comments.py`
- Modify: `books_of_time/db/repositories.py`
- Modify: `tests/test_comments_parser.py`
- Modify: `tests/test_comment_repositories.py`

**Interfaces:**
- Produces: `ParsedComment.platform_created_at: datetime | None`.
- Produces: stable parsed public fields matching the ORM names from Task 1.
- Produces: `ParsedComment.platform_time_evidence: dict[str, Any]` and `author_public_metadata_extra: dict[str, Any]`.
- Consumes: Bilibili reply `ctime`, `member.level_info`, `member.official_verify`, `member.vip`, `member.senior_member`, `member.nameplate`, and `member.pendant`.

- [ ] **Step 1: Write failing parser tests**

Extend the hot/latest parser fixtures with:

```python
"ctime": 1783490000,
"member": {
    "mid": "42",
    "uname": "Alice",
    "level_info": {"current_level": 6},
    "official_verify": {"type": 0, "desc": "Official account"},
    "vip": {"status": 1, "type": 2},
    "senior_member": {"status": 1},
    "nameplate": {"nid": 8, "name": "Collector"},
    "pendant": {"pid": 9, "name": "Badge"},
},
```

Assert UTC conversion, every scalar field, and an allowlisted extra object containing only `schema_version`, `nameplate`, and `pendant`. Add missing and malformed `ctime` cases asserting NULL plus evidence reasons `missing` and `invalid`.

- [ ] **Step 2: Run parser tests to verify RED**

```powershell
uv run pytest tests/test_comments_parser.py -q
```

Expected: `ParsedComment` lacks the new fields.

- [ ] **Step 3: Implement parser helpers and bump version**

Set `COMMENT_PARSER_VERSION = "comments.v3"`. Add helpers with these exact contracts:

```python
def _parse_platform_created_at(value: Any) -> tuple[datetime | None, dict[str, Any]]:
    if value is None:
        return None, {"status": "missing"}
    try:
        timestamp = int(value)
        if timestamp <= 0:
            raise ValueError
        return datetime.fromtimestamp(timestamp, tz=UTC), {"status": "parsed"}
    except (TypeError, ValueError, OSError, OverflowError):
        return None, {"status": "invalid", "raw_type": type(value).__name__}
```

Parse integer fields without treating zero as missing. For senior membership, accept a mapping `status` or scalar and return `None` when absent. Allowlist only bounded nameplate/pendant keys; do not structure IP location, sign, avatar URL, Cookie, or profile fields.

- [ ] **Step 4: Write failing repository tests**

Add a test that upserts two observations for one RPID. The first has missing entity evidence; the second has parsed evidence. Assert the entity fills only NULL fields, preserves first content/name, and both observations retain their own platform/member snapshots.

- [ ] **Step 5: Run repository test to verify RED**

```powershell
uv run pytest tests/test_comment_repositories.py -q
```

Expected: ORM rows do not receive the parsed fields.

- [ ] **Step 6: Persist observation and fill-only-missing entity fields**

Populate all new `CommentObservation` columns. Merge parser timing evidence into observation `extra` under `platform_time_evidence` while retaining `visibility_evidence`.

In `_ensure_entity`, when an entity exists, call a helper that assigns scalar fields only when the stored value is `None`. Merge only absent keys into `author_public_metadata_extra`; never replace existing keys. For a new entity, copy all first-known public fields directly.

- [ ] **Step 7: Verify comment evidence GREEN**

```powershell
uv run pytest tests/test_comments_parser.py tests/test_comment_repositories.py tests/test_hot_comments_worker.py tests/test_latest_comments_worker.py tests/test_reply_comments_worker.py -q
uv run ruff check books_of_time/parsers/comments.py books_of_time/db/repositories.py tests/test_comments_parser.py tests/test_comment_repositories.py
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```powershell
git add books_of_time/parsers/comments.py books_of_time/db/repositories.py tests/test_comments_parser.py tests/test_comment_repositories.py
git commit -m "feat(comments): preserve platform and public author evidence"
```

---

### Task 3: Persist Every Video Discovery Source

**Files:**
- Modify: `books_of_time/task_orchestrator/discovery_loop.py`
- Modify: `books_of_time/task_orchestrator/discovery_sources.py`
- Modify: `books_of_time/task_orchestrator/discovery.py`
- Modify: `books_of_time/parsers/discovery.py`
- Modify: `books_of_time/service/scheduled_jobs.py`
- Modify: `books_of_time/collectors/user_videos.py`
- Modify: `books_of_time/db/repositories.py`
- Modify: `config/config.yaml.example`
- Modify: `tests/test_config_loader.py`
- Modify: `tests/test_discovery_scheduler.py`
- Modify: `tests/test_service_scheduled_handlers.py`
- Modify: `tests/test_user_videos_worker.py`

**Interfaces:**
- Produces: `DiscoveryUidSource.as_payload() -> dict[str, str | bool | None]`.
- Produces task payload `source_associations: list[dict]` sorted by stable identity.
- Produces: `KnownVideoSourceRepository.upsert_for_video(...) -> list[KnownVideoSource]`.
- Consumes legacy scalar `source_pool_type/source_pool_id` payloads as a fallback during migration.

- [ ] **Step 1: Write failing source-resolution and merge tests**

Update the example-config expectation so every game pool is explicit:

```yaml
genshin_impact:
  game_id: genshin_impact
  official: true
  monitored: true
  uids: [401742377]
```

Add tests that two pools containing the same MID produce one discovery task with two `source_associations`, not a first-source-only payload. Assert matrix fallback uses `pool_id="matrix"`, `official=False`, `monitored=True`; a game pool defaults `game_id` to its pool key and both booleans to true.

- [ ] **Step 2: Run source tests to verify RED**

```powershell
uv run pytest tests/test_config_loader.py tests/test_service_scheduled_handlers.py -q
```

Expected: `DiscoveryUidSource` lacks source metadata and the handler keeps only one source via `setdefault`.

- [ ] **Step 3: Implement normalized source payloads**

Extend `DiscoveryUidSource`:

```python
@dataclass(frozen=True)
class DiscoveryUidSource:
    mid: str
    pool_type: str = "matrix"
    pool_id: str = "matrix"
    game_id: str | None = None
    official: bool = False
    monitored: bool = True

    def as_payload(self) -> dict[str, str | bool | None]:
        return {
            "source_mid": self.mid,
            "pool_type": self.pool_type,
            "pool_id": self.pool_id,
            "game_id": self.game_id,
            "official": self.official,
            "monitored": self.monitored,
        }
```

Validate non-empty IDs. In `UidDiscoveryScheduleHandler`, group all configured and event sources by MID, deduplicate by `(pool_type, pool_id, game_id, official, monitored)`, sort them, and put the full list in `source_associations`. Retain legacy scalar fields using the first sorted source for backward-compatible diagnostics.

For active event UID targets use `pool_type="event"`,
`pool_id=f"target:{target.id}"`, `game_id=None`, `monitored=True`, and
`official=(target.extra.get("role") == "official")`. A `major_creator` role is
not silently converted to official. YAML `official` and `monitored` values must
already be booleans; reject strings such as `"false"` instead of applying
Python truthiness.

- [ ] **Step 4: Write failing persistence/provenance tests**

Extend scheduler and worker tests to collect the same BVID twice from two associations. Assert two `KnownVideoSource` rows share the BVID, repeated discovery updates `last_seen_at/last_raw_page_id`, and only one video stats task exists. Assert `first_raw_page_id` is the discovery page that first observed each association.

- [ ] **Step 5: Run persistence tests to verify RED**

```powershell
uv run pytest tests/test_discovery_scheduler.py tests/test_user_videos_worker.py -q
```

Expected: `known_video_sources` remains empty.

- [ ] **Step 6: Implement source upsert and collector propagation**

Add repository method:

```python
async def upsert_for_video(
    self,
    *,
    bvid: str,
    associations: list[dict[str, Any]],
    seen_at: datetime,
    raw_page_observation_id: int | None,
) -> list[KnownVideoSource]: ...
```

Normalize `pool_id` to a non-empty string, create rows with first/last provenance, and update only `last_seen_at`, `last_raw_page_id`, `active`, and `updated_at` on repeat. Do not rewrite first provenance.

Pass `raw_page_observation_id` and normalized associations into `DiscoveryScheduler.handle_discovered_videos`. Keep `KnownVideo.source_mid` as first-source compatibility data and never overwrite it. Update raw-page `extra` and downstream stats-task payload with the full association list.

- [ ] **Step 7: Verify discovery GREEN**

```powershell
uv run pytest tests/test_config_loader.py tests/test_discovery_loop.py tests/test_discovery_scheduler.py tests/test_service_scheduled_handlers.py tests/test_user_videos_worker.py -q
uv run ruff check books_of_time/task_orchestrator books_of_time/service/scheduled_jobs.py books_of_time/collectors/user_videos.py books_of_time/db/repositories.py tests/test_discovery_scheduler.py tests/test_service_scheduled_handlers.py tests/test_user_videos_worker.py
```

Expected: all selected tests pass and repeated MID/BVID discovery retains all associations.

- [ ] **Step 8: Commit**

```powershell
git add books_of_time/task_orchestrator books_of_time/service/scheduled_jobs.py books_of_time/collectors/user_videos.py books_of_time/db/repositories.py config/config.yaml.example tests/test_config_loader.py tests/test_discovery_scheduler.py tests/test_service_scheduled_handlers.py tests/test_user_videos_worker.py
git commit -m "feat(discovery): preserve every video source association"
```

---

### Task 4: Add HTTP Attempt Repository Semantics

**Files:**
- Create: `tests/test_http_request_attempts.py`
- Modify: `books_of_time/db/repositories.py`
- Modify: `books_of_time/http/client.py`

**Interfaces:**
- Produces: `HttpRequestAttemptRepository.begin(...) -> HttpRequestAttempt`.
- Produces: `record_response(...)`, `record_transport_failure(...)`, `attach_raw_payload(...)`, and `mark_abandoned(...)`.
- Produces new optional `FetchResult` fields: `request_started_at`, `request_finished_at`, `response_received_at`, and `http_attempt_id`.

- [ ] **Step 1: Write failing repository lifecycle tests**

Cover these transitions:

```text
begin -> started/no raw
started + successful raw -> succeeded/raw linked
started + 429 raw -> failed/raw linked/error_type=429
started + timeout -> failed/no raw
started + collector abort -> abandoned
```

Also assert URL and canonical params are SHA-256 hashes and plaintext URL/params do not appear in the row.

- [ ] **Step 2: Run lifecycle test to verify RED**

```powershell
uv run pytest tests/test_http_request_attempts.py -q
```

Expected: repository class is missing.

- [ ] **Step 3: Implement repository methods**

`begin` receives task ID, request type, method, URL, params, and timing. It stores only hashes and flushes before network I/O. `record_response` stores HTTP/timing fields but keeps status `started` until raw is linked. `attach_raw_payload` sets raw ID and terminal status. Error messages are stripped and bounded to 2000 characters.

Extend `RawPayloadRepository.insert_from_fetch_result` with keyword `attempt_status: str = "succeeded"`; when `result.http_attempt_id` exists, attach the new raw row to that attempt in the same transaction.

- [ ] **Step 4: Verify repository GREEN**

```powershell
uv run pytest tests/test_http_request_attempts.py tests/test_comment_repositories.py tests/test_media_downloader.py -q
uv run ruff check books_of_time/db/repositories.py books_of_time/http/client.py tests/test_http_request_attempts.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add books_of_time/db/repositories.py books_of_time/http/client.py tests/test_http_request_attempts.py
git commit -m "feat(http): add request attempt lifecycle"
```

---

### Task 5: Capture Transport Failures And Archive Failed Bodies

**Files:**
- Create: `books_of_time/http/evidence.py`
- Create: `books_of_time/db/http_evidence.py`
- Modify: `books_of_time/http/client.py`
- Modify: `books_of_time/http/errors.py`
- Modify: `books_of_time/worker.py`
- Modify: `books_of_time/app.py`
- Modify: `tests/test_http_request_attempts.py`
- Modify: `tests/test_request_errors.py`
- Modify: `tests/test_worker_coverage.py`

**Interfaces:**
- Produces protocol `HttpEvidenceSink` with async `begin`, `record_response`, and `record_transport_failure` methods.
- Produces `capture_http_evidence(sink)` context manager and `current_http_evidence_sink()` accessor.
- Produces `DatabaseHttpEvidenceSink(session, raw_store, run_id, collection_task_id)`.
- Consumes: the current worker `AsyncSession`, `RawPayloadStore`, run ID, and task ID.

- [ ] **Step 1: Write failing integration tests**

Use a fake curl-cffi session and real in-memory ORM/raw filesystem to assert:

1. HTTP 429 creates one failed attempt and one linked raw payload before `RequestFailure` reaches the worker.
2. HTTP 500/captcha body follows the same path.
3. `TimeoutError` and a generic network exception create failed attempts without raw payloads.
4. A successful collector response becomes `succeeded` only after its existing raw insertion.
5. Calling `RawHttpClient` outside `capture_http_evidence` creates no attempt row, preserving login independence.

- [ ] **Step 2: Run integration tests to verify RED**

```powershell
uv run pytest tests/test_http_request_attempts.py tests/test_request_errors.py -q
```

Expected: no context sink exists and failed bodies are not archived.

- [ ] **Step 3: Implement the context protocol without DB imports**

`books_of_time/http/evidence.py` must use `Protocol`, `ContextVar`, and `contextmanager`; it must not import ORM models or repositories, avoiding an `http.client -> evidence -> repositories -> http.client` cycle.

The sink receives method/URL/params but never headers, cookies, or request body. `capture_http_evidence(None)` is a no-op-compatible context for workers without a raw store in unit tests.

- [ ] **Step 4: Implement the database sink**

`DatabaseHttpEvidenceSink` uses the current worker session. For a failed response it must:

```text
record response timing/status
-> raw_store.save(body, captured_at, run_id, suffix)
-> RawPayloadRepository.insert_from_fetch_result(..., attempt_status="failed")
-> flush
-> allow RequestFailure propagation
```

Use `.json` when Content-Type contains `json`, otherwise `.bin`. A raw storage error must not be hidden; leave the attempt non-successful and re-raise.

- [ ] **Step 5: Integrate RawHttpClient and worker**

In `RawHttpClient.request`, obtain the current sink after managed Cookie assembly. Call `begin` immediately before opening the network session (Bilibili/media token acquisition already happened outside this method). Capture actual start/finish timestamps. On response, build `FetchResult` with attempt ID, call `record_response`, classify failure, then raise. On transport exception, call `record_transport_failure` before raising typed `RequestFailure`.

Add `RequestErrorKind.NETWORK = "network"` and a 60-second worker default backoff. Do not catch `asyncio.CancelledError`.

Give `Worker` an optional `raw_store`; when present, wrap each collector call with a `DatabaseHttpEvidenceSink`. On collector failure, mark any still-started attempts for that task `abandoned` before committing failure coverage. `build_worker` passes its existing `raw_store`; direct Worker tests remain compatible with `None`.

- [ ] **Step 6: Verify HTTP evidence GREEN**

```powershell
uv run pytest tests/test_http_request_attempts.py tests/test_request_errors.py tests/test_bilibili_client.py tests/test_worker_coverage.py tests/test_media_downloader.py -q
uv run ruff check books_of_time/http books_of_time/db/http_evidence.py books_of_time/worker.py books_of_time/app.py tests/test_http_request_attempts.py tests/test_request_errors.py tests/test_worker_coverage.py
```

Expected: failed response bodies resolve through `http_request_attempts.raw_payload_id`; transport failures have no raw ID; no secret-bearing values are stored.

- [ ] **Step 7: Commit**

```powershell
git add books_of_time/http books_of_time/db/http_evidence.py books_of_time/worker.py books_of_time/app.py tests/test_http_request_attempts.py tests/test_request_errors.py tests/test_worker_coverage.py
git commit -m "feat(http): preserve failed request evidence"
```

---

### Task 6: Document, Migrate, And Close C1

**Files:**
- Modify: `docs/CONFIGURATION.md`
- Modify: `docs/COLLECTION.md`
- Modify: `docs/DATA_MODEL.md`
- Modify: `docs/TODO.md`

**Interfaces:**
- Produces operator-visible semantics for comment timestamps, source associations, and HTTP attempts.
- Produces C1 completion evidence; C2 remains unstarted.

- [ ] **Step 1: Update operator documentation**

Document:

- game pool `game_id/official/monitored` defaults and multi-pool MID merging;
- `platform_created_at` versus `first_seen_at` versus `captured_at`;
- allowlisted public author fields and the explicit raw-only exclusion boundary;
- `known_video_sources` many-to-many identity and first/last raw provenance;
- attempt statuses/timestamps, failed-body archival, timeout/network no-body evidence, and secret exclusions;
- C1's current-session limitation and that C7 will move attempt/page work to fully durable short transactions.

- [ ] **Step 2: Run isolated migration cycle and full verification**

Run:

```powershell
uv run pytest tests/test_schema_migrations.py::test_collection_evidence_revision_round_trip -q
uv run pytest
uv run ruff check .
git diff --check
```

Expected: the isolated migration cycle succeeds, all tests pass, Ruff passes,
and Git reports no whitespace errors. Do not run downgrade against the user's
configured PostgreSQL database. A PostgreSQL round trip, when available, must
use a disposable database or isolated schema created for this test.

- [ ] **Step 3: Mark only C1 complete in TODO**

Change C1 from `[~]` to `[x]`. Leave C2-C9 unchecked. Do not mark the overall P1 collection-cohort mainline complete.

- [ ] **Step 4: Commit**

```powershell
git add docs/CONFIGURATION.md docs/COLLECTION.md docs/DATA_MODEL.md docs/TODO.md
git commit -m "docs: document collection evidence foundations"
```

## Plan Self-Review Result

- Spec coverage: C1 covers sections 6.7, 6.10, 6.11, and the failed-body/request-attempt portion of section 12. Cohort/scan/visibility/capacity policy remains mapped to C2-C9 in `docs/TODO.md`.
- Placeholder scan: clean; every task names its concrete behavior and command.
- Type consistency: parser, ORM, repository, discovery payload, and HTTP sink names are defined once above and reused consistently.
- Execution choice: prior user direction selects inline main-thread execution with staged verification; implementation subagents are not required.
