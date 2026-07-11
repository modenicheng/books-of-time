# Scaling Decisions

PostgreSQL remains the evidence system of record. Any future time-series,
analytics, or search system is a rebuildable projection. Every projected record
must retain `raw_payload_id` and, where applicable, `comment_observation_id` so an
operator can return to the collected evidence.

Detailed rationale and official references are recorded in
[SCALING_EVALUATION](SCALING_EVALUATION.md).

## TimescaleDB

**Decision: Do not adopt now.**

- **Trigger:** a time table exceeds 500 million rows or 1 TiB, native partition
  pruning no longer controls query cost, or aggregate refresh consumes more than
  20% of database CPU.
- **Benchmark gate:** representative time-bucket queries remain above 5 seconds
  p95 after native monthly partitioning, BRIN indexes, and explicit rollups.
- **Rollback:** keep the original PostgreSQL tables writable until hypertable
  results and evidence joins match; disable projection writes and return reads to
  the original tables without changing raw evidence.

## ClickHouse

**Decision: Do not adopt now.**

- **Trigger:** analytical scans exceed 100 million rows, consume over 30% of
  PostgreSQL CPU or I/O, or block collector write latency under expected
  concurrency.
- **Benchmark gate:** representative reports remain above 10 seconds p95 and a
  CDC-backed ClickHouse prototype meets the same evidence counts and IDs within a
  declared replication-lag SLA.
- **Rollback:** stop CDC consumers, discard the analytical replica, and route all
  reports to PostgreSQL. The application never dual-writes ClickHouse as a source
  of truth.

## OpenSearch

**Decision: Do not adopt now.**

- **Trigger:** product search requires complex query DSL, multi-field relevance,
  aggregations, highlighting, and independent index lifecycle management.
- **Benchmark gate:** PostgreSQL FTS and `pg_trgm` exceed 500 ms p95 on
  representative Chinese text and expected concurrency, while an OpenSearch
  prototype meets relevance and evidence-link acceptance tests.
- **Rollback:** stop indexing, remove the read route, and rebuild or discard the
  index. PostgreSQL rows and raw payloads remain untouched.

## Meilisearch

**Decision: Do not adopt now.**

- **Trigger:** a lightweight user-facing search experience needs typo tolerance,
  prefix search, and tunable ranking without OpenSearch's operational scope.
- **Benchmark gate:** PostgreSQL search exceeds 500 ms p95 and a representative
  relevance set shows a measurable improvement without losing structured event
  and time filters.
- **Rollback:** disable the search adapter and discard the index; rebuild is always
  possible from PostgreSQL using stable evidence IDs.
