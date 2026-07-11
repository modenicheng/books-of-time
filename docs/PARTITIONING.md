# Comment Observation Monthly Partition Plan

## Status

`comment_observations` remains a regular table today. The project has a tested UTC
month-boundary and DDL generator, but it deliberately does not convert an existing
table in place. Operators must not run generated `PARTITION OF` statements until
the v2 parent described here exists.

This split is intentional. PostgreSQL requires every primary or unique constraint
on a partitioned parent to include the partition key. The current table uses
`PRIMARY KEY (id)`, while monthly range partitioning requires `captured_at` as the
partition key. Pretending this is an online `ALTER TABLE` would weaken the evidence
identity contract and can break references.

## Target Schema

The target parent is created alongside the current table:

```sql
CREATE TABLE comment_observations_v2 (
    LIKE comment_observations INCLUDING DEFAULTS INCLUDING GENERATED
) PARTITION BY RANGE (captured_at);

ALTER TABLE comment_observations_v2
    ADD PRIMARY KEY (captured_at, id);
```

The `id` column continues to use one shared sequence so IDs remain operationally
unique. PostgreSQL cannot enforce uniqueness of `id` alone across range partitions,
so application lookups and related records must carry `captured_at` as part of the
formal identity before cutover.

Monthly children use half-open UTC bounds. For example:

```sql
CREATE TABLE comment_observations_y2026m12
PARTITION OF comment_observations_v2
FOR VALUES FROM ('2026-12-01T00:00:00+00:00')
TO ('2027-01-01T00:00:00+00:00');
```

A temporary DEFAULT partition is required during rollout so an unexpected timestamp
cannot stop collection. The maintenance monitor must alert whenever the DEFAULT
partition receives rows; those rows are moved only after the missing month is
created and validated.

## Dependent Identity Changes

Before switching the parent, add observation timestamps beside observation IDs in:

- `comment_observation_media.comment_observation_id`
- `comment_state_events.previous_comment_observation_id`
- `comment_state_events.current_comment_observation_id`
- `comment_visibility_events.previous_comment_observation_id`
- `comment_visibility_events.current_comment_observation_id`

Repository APIs that fetch an observation by ID must accept `(captured_at, id)` or a
bounded time range. Reports and raw evidence exports must retain both values. This
change is a prerequisite for a real composite foreign key and effective partition
pruning.

## Rollout

1. Measure table size, write rate, longest queries, and available disk headroom.
2. Create `comment_observations_v2`, monthly children, and a DEFAULT partition.
3. Deploy dual-write from the normalizer in one transaction; keep reads on v1.
4. Backfill month by month in bounded batches ordered by `(captured_at, id)`.
5. Validate per-month row counts, min/max timestamps, content hashes, media hashes,
   and sampled raw evidence references.
6. Backfill timestamp identity columns in dependent tables and validate joins.
7. Shadow-read v2 and compare query results and reports with v1.
8. Pause new leases briefly, drain active writers, copy the tail, and switch reads.
9. Keep v1 read-only through the rollback window; do not drop it in the cutover.
10. After the retention window and a restore drill, archive or drop v1 explicitly.

## Rollback

During dual-write and shadow-read, rollback means disabling v2 writes and continuing
to read v1. After read cutover, rollback requires a short write pause, copying the v2
tail back to v1, validating counts and hashes, then restoring v1 reads. The old table
must remain untouched until that rollback has been rehearsed.

## Ongoing Maintenance

Create the current month plus at least three future partitions. Run the planner on a
schedule and before month end. Partition creation is idempotent, but execution must
first verify that the target parent is actually range-partitioned on `captured_at`.
Never interpolate operator-provided table names into DDL.

Detach or remove old partitions only under an explicit retention policy. Event
archive evidence, report citations, pinned media relationships, and PostgreSQL/raw
storage backups must be checked before any destructive lifecycle action.
