from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from books_of_time.db.partitioning import iter_comment_observation_partitions

_TIME_TABLES = (
    "raw_payloads",
    "raw_page_observations",
    "comment_observations",
    "video_metric_snapshots",
    "video_info_snapshots",
    "video_availability_snapshots",
    "comment_state_events",
    "comment_visibility_events",
)

_BRIN_INDEXES = (
    "idx_raw_payloads_captured_brin",
    "idx_raw_page_observations_captured_brin",
    "idx_comment_observations_captured_brin",
    "idx_video_metric_snapshots_captured_brin",
    "idx_video_info_snapshots_captured_brin",
    "idx_video_availability_snapshots_captured_brin",
    "idx_comment_state_events_created_brin",
    "idx_comment_visibility_events_created_brin",
)


@dataclass(frozen=True, slots=True)
class MaintenanceAction:
    kind: str
    target: str
    sql: str | None
    status: str
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": "database-maintenance-action-v1"} | asdict(self)


class DatabaseMaintenanceService:
    def __init__(self, engine: AsyncEngine | None) -> None:
        self.engine = engine

    async def run(
        self,
        *,
        now: datetime,
        execute: bool,
        vacuum: bool,
        months_ahead: int,
    ) -> tuple[MaintenanceAction, ...]:
        if self.engine is None:
            raise RuntimeError("Database engine is required to run maintenance")
        dialect_name = self.engine.dialect.name
        partition_parent_ready = (
            await self._partition_parent_ready()
            if dialect_name == "postgresql"
            else False
        )
        plan = self.build_plan(
            dialect_name=dialect_name,
            now=now,
            vacuum=vacuum,
            months_ahead=months_ahead,
            partition_parent_ready=partition_parent_ready,
        )
        if not execute:
            return plan

        results: list[MaintenanceAction] = []
        for index, action in enumerate(plan):
            if action.status == "skipped":
                results.append(action)
                continue
            assert action.sql is not None
            try:
                async with self.engine.connect() as connection:
                    autocommit = await connection.execution_options(
                        isolation_level="AUTOCOMMIT"
                    )
                    await autocommit.execute(text(action.sql))
            except Exception as exc:
                failed = replace(
                    action,
                    status="failed",
                    reason=f"{type(exc).__name__}: {exc}"[:500],
                )
                results.append(failed)
                results.extend(
                    replace(
                        remaining,
                        status="skipped",
                        reason="not run after previous maintenance failure",
                    )
                    for remaining in plan[index + 1 :]
                )
                break
            results.append(replace(action, status="executed"))
        return tuple(results)

    def build_plan(
        self,
        *,
        dialect_name: str,
        now: datetime,
        vacuum: bool,
        months_ahead: int,
        partition_parent_ready: bool,
    ) -> tuple[MaintenanceAction, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include a timezone offset")
        if not 0 <= months_ahead <= 24:
            raise ValueError("months_ahead must be between 0 and 24")
        normalized_dialect = dialect_name.casefold()
        actions: list[MaintenanceAction] = []
        if normalized_dialect == "postgresql":
            maintenance_kind = "vacuum_analyze" if vacuum else "analyze"
            for table_name in _TIME_TABLES:
                sql = (
                    f"VACUUM (ANALYZE) {table_name}"
                    if vacuum
                    else f"ANALYZE {table_name}"
                )
                actions.append(
                    MaintenanceAction(
                        kind=maintenance_kind,
                        target=table_name,
                        sql=sql,
                        status="planned",
                    )
                )
        elif normalized_dialect == "sqlite":
            actions.append(
                MaintenanceAction(
                    kind="analyze",
                    target="database",
                    sql="ANALYZE",
                    status="planned",
                )
            )
        else:
            raise ValueError(f"Unsupported maintenance dialect: {dialect_name}")

        if normalized_dialect == "postgresql" and partition_parent_ready:
            actions.extend(
                MaintenanceAction(
                    kind="partition",
                    target=partition.name,
                    sql=partition.create_sql(),
                    status="planned",
                )
                for partition in iter_comment_observation_partitions(
                    now.astimezone(UTC),
                    months_ahead=months_ahead,
                )
            )
        else:
            actions.append(
                MaintenanceAction(
                    kind="partition",
                    target="comment_observations_v2",
                    sql=None,
                    status="skipped",
                    reason=(
                        "comment_observations_v2 is not range-partitioned on "
                        "captured_at"
                        if normalized_dialect == "postgresql"
                        else "monthly partitions are PostgreSQL-only"
                    ),
                )
            )

        if normalized_dialect == "postgresql":
            actions.extend(
                MaintenanceAction(
                    kind="brin_summarize",
                    target=index_name,
                    sql=(f"SELECT brin_summarize_new_values('{index_name}'::regclass)"),
                    status="planned",
                )
                for index_name in _BRIN_INDEXES
            )
        else:
            actions.append(
                MaintenanceAction(
                    kind="brin_summarize",
                    target="database",
                    sql=None,
                    status="skipped",
                    reason="BRIN indexes are PostgreSQL-only",
                )
            )
        return tuple(actions)

    async def _partition_parent_ready(self) -> bool:
        assert self.engine is not None
        query = text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_partitioned_table partitioned
                JOIN pg_class parent ON parent.oid = partitioned.partrelid
                JOIN pg_attribute attribute
                  ON attribute.attrelid = parent.oid
                 AND attribute.attnum = partitioned.partattrs[0]
                WHERE parent.oid = to_regclass('comment_observations')
                  AND partitioned.partstrat = 'r'
                  AND partitioned.partnatts = 1
                  AND partitioned.partexprs IS NULL
                  AND attribute.attname = 'captured_at'
            )
            """
        )
        async with self.engine.connect() as connection:
            return bool(await connection.scalar(query))
