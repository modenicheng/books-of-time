import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from books_of_time import cli
from books_of_time.db.maintenance import DatabaseMaintenanceService


def test_database_maintenance_parser_is_dry_run_by_default() -> None:
    dry_run = cli.build_parser().parse_args(["database", "maintain"])
    execute = cli.build_parser().parse_args(
        [
            "database",
            "maintain",
            "--execute",
            "--vacuum",
            "--months-ahead",
            "6",
            "--output",
            "maintenance.jsonl",
        ]
    )

    assert dry_run.database_command == "maintain"
    assert dry_run.execute is False
    assert execute.execute is True
    assert execute.vacuum is True
    assert execute.months_ahead == 6


@pytest.mark.asyncio
async def test_sqlite_maintenance_dry_run_and_execute_are_auditable(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'maintenance.sqlite3'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE sample (id INTEGER PRIMARY KEY)"))
        await connection.execute(text("INSERT INTO sample (id) VALUES (1)"))

    service = DatabaseMaintenanceService(engine)
    planned = await service.run(
        now=datetime(2026, 12, 15, tzinfo=UTC),
        execute=False,
        vacuum=False,
        months_ahead=3,
    )
    assert [(row.kind, row.status) for row in planned] == [
        ("analyze", "planned"),
        ("partition", "skipped"),
        ("brin_summarize", "skipped"),
    ]

    executed = await service.run(
        now=datetime(2026, 12, 15, tzinfo=UTC),
        execute=True,
        vacuum=False,
        months_ahead=3,
    )
    assert [(row.kind, row.status) for row in executed] == [
        ("analyze", "executed"),
        ("partition", "skipped"),
        ("brin_summarize", "skipped"),
    ]
    async with engine.connect() as connection:
        stats_table = await connection.scalar(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'sqlite_stat1'"
            )
        )
    assert stats_table == "sqlite_stat1"
    await engine.dispose()


@pytest.mark.asyncio
async def test_database_maintenance_cli_helper_writes_dry_run_jsonl(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'cli.sqlite3'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE sample (id INTEGER PRIMARY KEY)"))
    await engine.dispose()
    output = tmp_path / "maintenance.jsonl"

    actions = await cli._maintain_database(
        {"database": {"url": database_url}},
        execute=False,
        vacuum=False,
        months_ahead=3,
        output_path=output,
    )

    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(records) == len(actions)
    assert records[0]["schema_version"] == "database-maintenance-action-v1"
    assert {record["status"] for record in records} == {"planned", "skipped"}


@pytest.mark.asyncio
async def test_postgresql_plan_is_bounded_and_partition_safe() -> None:
    service = DatabaseMaintenanceService(None)
    now = datetime(2026, 12, 15, tzinfo=UTC)

    without_parent = service.build_plan(
        dialect_name="postgresql",
        now=now,
        vacuum=True,
        months_ahead=2,
        partition_parent_ready=False,
    )
    assert any(row.kind == "vacuum_analyze" for row in without_parent)
    assert sum(row.kind == "brin_summarize" for row in without_parent) == 8
    partition = next(row for row in without_parent if row.kind == "partition")
    assert partition.status == "skipped"
    assert "not range-partitioned" in (partition.reason or "")

    with_parent = service.build_plan(
        dialect_name="postgresql",
        now=now,
        vacuum=False,
        months_ahead=2,
        partition_parent_ready=True,
    )
    partition_sql = [row.sql for row in with_parent if row.kind == "partition"]
    assert len(partition_sql) == 3
    assert "comment_observations_y2026m12" in partition_sql[0]
    assert "comment_observations_y2027m02" in partition_sql[-1]
    assert all("CREATE TABLE IF NOT EXISTS" in statement for statement in partition_sql)
