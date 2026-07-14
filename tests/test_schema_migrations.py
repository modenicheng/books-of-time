import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex

from alembic import command
from books_of_time.db.base import Base
from books_of_time.db.migrations import (
    get_current_schema_revision,
    get_expected_schema_revision,
)
from books_of_time.db.schema import adopt_legacy_schema, create_schema
from books_of_time.service.health import ServiceHealthChecker


@pytest.mark.asyncio
async def test_schema_revision_helpers_read_expected_and_current_head(
    tmp_path: Path,
) -> None:
    expected = get_expected_schema_revision()
    assert expected == "0012_latest_comment_scans"

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": expected},
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        assert await get_current_schema_revision(session) == expected
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_doctor_rejects_missing_schema_revision(tmp_path: Path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    checker = ServiceHealthChecker(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        raw_dir=tmp_path / "raw",
        media_dir=tmp_path / "media",
        expected_schema_revision="0001_initial",
    )

    report = await checker.doctor()
    revision = next(check for check in report.checks if check.name == "schema_revision")

    assert report.ok is False
    assert revision.ok is False
    assert "missing" in revision.detail
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_revision", "expected_ok"),
    [("0001_initial", True), ("old_revision", False)],
)
async def test_service_doctor_compares_schema_revision(
    tmp_path: Path,
    stored_revision: str,
    expected_ok: bool,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": stored_revision},
        )
    checker = ServiceHealthChecker(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        raw_dir=tmp_path / "raw",
        media_dir=tmp_path / "media",
        expected_schema_revision="0001_initial",
    )

    report = await checker.doctor()
    revision = next(check for check in report.checks if check.name == "schema_revision")

    assert revision.ok is expected_ok
    assert report.ok is expected_ok
    await engine.dispose()


def test_initial_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0001_initial.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert "Base.metadata" not in source
    assert "def upgrade()" in source
    assert "def downgrade()" in source


def test_event_archive_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0002_event_archive.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert 'down_revision: str | Sequence[str] | None = "0001_initial"' in source
    assert "Base.metadata" not in source
    assert 'op.create_table(\n        "events"' in source
    assert 'op.create_table(\n        "event_targets"' in source
    assert 'op.create_table(\n        "event_videos"' in source
    assert 'op.create_table(\n        "event_keywords"' in source


def test_account_cookie_refresh_revision_extends_postgresql_enum_safely() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0003_account_cookie_refresh_job.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert 'down_revision: str | Sequence[str] | None = "0002_event_archive"' in source
    assert "ADD VALUE IF NOT EXISTS 'account_cookie_refresh'" in source
    assert "DELETE FROM scheduled_jobs" in source
    assert "Base.metadata" not in source


def test_comment_analysis_flags_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0004_comment_analysis_flags.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert (
        "down_revision: str | Sequence[str] | None = "
        '"0003_account_cookie_refresh_job"' in source
    )
    assert 'op.create_table(\n        "comment_analysis_flags"' in source
    assert 'op.create_index(\n        "idx_comment_analysis_flags_event_type"' in source
    assert "Base.metadata" not in source


def test_brin_time_index_revision_is_postgresql_only_and_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0005_brin_time_indexes.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert (
        'down_revision: str | Sequence[str] | None = "0004_comment_analysis_flags"'
        in source
    )
    assert 'dialect.name != "postgresql"' in source
    assert 'postgresql_using="brin"' in source
    assert '"autosummarize": True' in source
    assert "Base.metadata" not in source


def test_request_budget_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0006_request_budget_states.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert (
        'down_revision: str | Sequence[str] | None = "0005_brin_time_indexes"' in source
    )
    assert 'op.create_table(\n        "request_budget_states"' in source
    assert 'sa.PrimaryKeyConstraint("budget_key")' in source
    assert "Base.metadata" not in source


def test_operational_alert_revision_is_static_and_extends_job_kind() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0007_operational_alert_states.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert (
        'down_revision: str | Sequence[str] | None = "0006_request_budget_states"'
        in source
    )
    assert "ADD VALUE IF NOT EXISTS 'operational_alert_evaluation'" in source
    assert 'op.create_table(\n        "operational_alert_states"' in source
    assert 'sa.PrimaryKeyConstraint("alert_key")' in source
    assert 'sa.text("detected_at DESC")' in source
    assert 'comment="记录创建时间"' in source
    assert 'comment="记录最后更新时间"' in source
    assert "Base.metadata" not in source


def test_collection_evidence_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0008_collection_evidence_foundations.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert (
        "down_revision: str | Sequence[str] | None = "
        '"0007_operational_alert_states"' in source
    )
    assert 'op.create_table(\n        "known_video_sources"' in source
    assert 'op.create_table(\n        "http_request_attempts"' in source
    assert "Base.metadata" not in source


def test_collection_evidence_revision_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "evidence-cycle.sqlite3"
    config_path = _write_sqlite_config(
        tmp_path / "evidence-cycle.yaml",
        database_path,
    )
    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_config.attributes["bot_config_path"] = str(config_path)
    alembic_config.attributes["skip_logger_config"] = True

    command.upgrade(alembic_config, "head")
    assert _sqlite_table_exists(database_path, "known_video_sources")
    assert _sqlite_table_exists(database_path, "http_request_attempts")
    assert "platform_created_at" in _sqlite_columns(database_path, "comment_entities")

    command.downgrade(alembic_config, "0007_operational_alert_states")
    assert not _sqlite_table_exists(database_path, "known_video_sources")
    assert not _sqlite_table_exists(database_path, "http_request_attempts")
    assert "platform_created_at" not in _sqlite_columns(
        database_path,
        "comment_entities",
    )

    command.upgrade(alembic_config, "head")
    assert _sqlite_table_exists(database_path, "known_video_sources")
    assert _sqlite_table_exists(database_path, "http_request_attempts")


def test_cohort_state_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0009_cohort_state_and_policy.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert (
        "down_revision: str | Sequence[str] | None = "
        '"0008_collection_evidence_foundations"' in source
    )
    assert 'op.create_table(\n        "collection_policy_versions"' in source
    assert 'op.create_table(\n        "snapshot_cohorts"' in source
    assert 'op.add_column(\n        "collection_tasks"' in source
    assert "Base.metadata" not in source


def test_cohort_state_revision_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "cohort-state-cycle.sqlite3"
    config_path = _write_sqlite_config(
        tmp_path / "cohort-state-cycle.yaml",
        database_path,
    )
    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_config.attributes["bot_config_path"] = str(config_path)
    alembic_config.attributes["skip_logger_config"] = True

    command.upgrade(alembic_config, "head")
    assert _sqlite_table_exists(database_path, "collection_policy_versions")
    assert _sqlite_table_exists(database_path, "video_collection_states")
    assert _sqlite_table_exists(database_path, "snapshot_cohorts")
    assert _sqlite_table_exists(database_path, "snapshot_cohort_components")
    assert _sqlite_table_exists(database_path, "collection_schedule_gaps")
    assert "snapshot_cohort_id" in _sqlite_columns(
        database_path,
        "collection_tasks",
    )


def test_snapshot_cohort_planning_job_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0010_snapshot_cohort_planning_job.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert (
        "down_revision: str | Sequence[str] | None = "
        '"0009_cohort_state_and_policy"' in source
    )
    assert "ADD VALUE IF NOT EXISTS 'snapshot_cohort_planning'" in source
    assert "DELETE FROM scheduled_jobs" in source
    assert "Base.metadata" not in source


def test_snapshot_cohort_planning_job_revision_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "cohort-planner-job-cycle.sqlite3"
    config_path = _write_sqlite_config(
        tmp_path / "cohort-planner-job-cycle.yaml",
        database_path,
    )
    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_config.attributes["bot_config_path"] = str(config_path)
    alembic_config.attributes["skip_logger_config"] = True

    command.upgrade(alembic_config, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO scheduled_jobs (
                job_key,
                job_kind,
                schedule_seconds,
                priority,
                payload,
                enabled,
                next_run_at,
                consecutive_failures,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                "snapshot-cohort-planning",
                "snapshot_cohort_planning",
                30,
                110,
                "{}",
                1,
                "2026-07-14 04:00:00+00:00",
                0,
            ),
        )
        connection.commit()

    command.downgrade(alembic_config, "0009_cohort_state_and_policy")
    with sqlite3.connect(database_path) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM scheduled_jobs "
            "WHERE job_kind = 'snapshot_cohort_planning'"
        ).fetchone()
    assert remaining == (0,)

    command.upgrade(alembic_config, "head")
    assert _sqlite_table_exists(database_path, "snapshot_cohorts")
    assert "snapshot_cohort_component_id" in _sqlite_columns(
        database_path,
        "collection_coverage_stats",
    )

    command.downgrade(alembic_config, "0008_collection_evidence_foundations")
    assert not _sqlite_table_exists(database_path, "collection_policy_versions")
    assert not _sqlite_table_exists(database_path, "snapshot_cohorts")
    assert "snapshot_cohort_id" not in _sqlite_columns(
        database_path,
        "collection_tasks",
    )
    assert "snapshot_cohort_component_id" not in _sqlite_columns(
        database_path,
        "collection_coverage_stats",
    )

    command.upgrade(alembic_config, "head")
    assert _sqlite_table_exists(database_path, "snapshot_cohorts")
    assert "snapshot_cohort_id" in _sqlite_columns(
        database_path,
        "collection_tasks",
    )


def test_hot_comment_scan_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0011_hot_comment_scans.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert (
        "down_revision: str | Sequence[str] | None = "
        '"0010_snapshot_cohort_planning_job"' in source
    )
    assert 'op.create_table(\n        "comment_scan_runs"' in source
    assert 'op.batch_alter_table("collection_tasks")' in source
    assert 'batch_op.add_column(sa.Column("comment_scan_run_id"' in source
    assert "scan_slice_key" in source
    assert "Base.metadata" not in source


def test_first_long_revision_widens_postgresql_alembic_version_column() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0008_collection_evidence_foundations.py"
    )
    source = revision_path.read_text(encoding="utf-8")
    widen_call = 'op.alter_column(\n            "alembic_version"'
    first_schema_change = '_add_comment_evidence_columns("comment_entities")'

    assert len("0008_collection_evidence_foundations") > 32
    assert widen_call in source
    assert "type_=sa.String(length=128)" in source
    assert source.index(widen_call) < source.index(first_schema_change)


def test_hot_comment_scan_revision_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "hot-comment-scan-cycle.sqlite3"
    config_path = _write_sqlite_config(
        tmp_path / "hot-comment-scan-cycle.yaml",
        database_path,
    )
    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_config.attributes["bot_config_path"] = str(config_path)
    alembic_config.attributes["skip_logger_config"] = True

    command.upgrade(alembic_config, "head")
    assert _sqlite_table_exists(database_path, "comment_scan_runs")


def test_latest_comment_scan_revision_is_static() -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0012_latest_comment_scans.py"
    )
    source = revision_path.read_text(encoding="utf-8")

    assert (
        'down_revision: str | Sequence[str] | None = "0011_hot_comment_scans"' in source
    )
    assert "active_scan_run_id" in source
    assert "frontier_anchor_set" in source
    assert "uq_comment_scan_runs_active_latest_bvid" in source
    assert "Base.metadata" not in source


def test_latest_comment_scan_revision_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "latest-comment-scan-cycle.sqlite3"
    config_path = _write_sqlite_config(
        tmp_path / "latest-comment-scan-cycle.yaml",
        database_path,
    )
    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_config.attributes["bot_config_path"] = str(config_path)
    alembic_config.attributes["skip_logger_config"] = True

    command.upgrade(alembic_config, "0011_hot_comment_scans")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO frontier_states (
                target_type,
                target_id,
                frontier_type,
                frontier_rpid,
                frontier_time,
                cursor,
                last_scan_at,
                last_scan_status,
                last_scan_pages,
                last_scan_truncated,
                extra
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "video",
                "BV-MIGRATED-FRONTIER",
                "latest_comments",
                1001,
                "2026-07-14 08:00:00+00:00",
                None,
                "2026-07-14 08:00:00+00:00",
                "baseline_complete",
                1,
                0,
                '{"baseline_status": "baseline_complete"}',
            ),
        )
        connection.commit()

    command.upgrade(alembic_config, "head")
    assert {
        "active_scan_run_id",
        "version",
        "frontier_anchor_set",
    }.issubset(_sqlite_columns(database_path, "frontier_states"))
    assert "idx_frontier_states_active_scan" in _sqlite_indexes(
        database_path,
        "frontier_states",
    )
    assert "uq_comment_scan_runs_active_latest_bvid" in _sqlite_indexes(
        database_path,
        "comment_scan_runs",
    )
    with sqlite3.connect(database_path) as connection:
        migrated_anchors = connection.execute(
            "SELECT frontier_anchor_set FROM frontier_states "
            "WHERE target_id = 'BV-MIGRATED-FRONTIER'"
        ).fetchone()
    assert migrated_anchors is not None
    assert json.loads(migrated_anchors[0]) == [
        {"rpid": 1001, "platform_created_at": None}
    ]

    command.downgrade(alembic_config, "0011_hot_comment_scans")
    assert "active_scan_run_id" not in _sqlite_columns(
        database_path,
        "frontier_states",
    )
    assert "idx_frontier_states_active_scan" not in _sqlite_indexes(
        database_path,
        "frontier_states",
    )
    assert "uq_comment_scan_runs_active_latest_bvid" not in _sqlite_indexes(
        database_path,
        "comment_scan_runs",
    )

    command.upgrade(alembic_config, "head")
    assert "frontier_anchor_set" in _sqlite_columns(
        database_path,
        "frontier_states",
    )
    assert {
        "comment_scan_run_id",
        "scan_slice_no",
        "scan_slice_key",
    }.issubset(_sqlite_columns(database_path, "collection_tasks"))
    assert "comment_scan_run_id" in _sqlite_columns(
        database_path,
        "collection_coverage_stats",
    )
    assert "scan_run_id" in _sqlite_columns(database_path, "raw_page_observations")
    assert "scan_run_id" in _sqlite_columns(database_path, "comment_observations")
    assert "idx_snapshot_cohort_components_scan_run" in _sqlite_indexes(
        database_path,
        "snapshot_cohort_components",
    )

    command.downgrade(alembic_config, "0010_snapshot_cohort_planning_job")
    assert not _sqlite_table_exists(database_path, "comment_scan_runs")
    assert "scan_slice_key" not in _sqlite_columns(
        database_path,
        "collection_tasks",
    )
    assert "comment_scan_run_id" not in _sqlite_columns(
        database_path,
        "collection_coverage_stats",
    )
    assert "scan_run_id" not in _sqlite_columns(
        database_path,
        "raw_page_observations",
    )
    assert "scan_run_id" not in _sqlite_columns(
        database_path,
        "comment_observations",
    )
    assert "idx_snapshot_cohort_components_scan_run" not in _sqlite_indexes(
        database_path,
        "snapshot_cohort_components",
    )

    command.upgrade(alembic_config, "head")
    assert _sqlite_table_exists(database_path, "comment_scan_runs")


def test_large_time_indexes_compile_as_postgresql_brin() -> None:
    expected = {
        "idx_raw_payloads_captured_brin",
        "idx_raw_page_observations_captured_brin",
        "idx_comment_observations_captured_brin",
        "idx_video_metric_snapshots_captured_brin",
        "idx_video_info_snapshots_captured_brin",
        "idx_video_availability_snapshots_captured_brin",
        "idx_comment_state_events_created_brin",
        "idx_comment_visibility_events_created_brin",
    }
    indexes = {
        index.name: index
        for table in Base.metadata.tables.values()
        for index in table.indexes
        if index.name in expected
    }

    assert set(indexes) == expected
    for index in indexes.values():
        statement = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        assert " USING brin " in statement
        assert "autosummarize = True" in statement
        assert "pages_per_range = 128" in statement


def test_importing_migration_helpers_does_not_load_autogenerate_plugins() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import books_of_time.db.migrations; "
            "print(any(name.startswith('alembic.autogenerate') "
            "for name in sys.modules))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "False"


@pytest.mark.asyncio
async def test_create_schema_uses_alembic_head(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.sqlite3"
    config_path = _write_sqlite_config(tmp_path / "fresh.yaml", database_path)

    await create_schema(str(config_path))

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        assert (
            await get_current_schema_revision(session) == get_expected_schema_revision()
        )
        table = await session.scalar(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'comment_analysis_flags'"
            )
        )
        sqlite_brin_count = await session.scalar(
            text(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'index' AND name LIKE '%_brin'"
            )
        )
    await engine.dispose()
    assert table == "comment_analysis_flags"
    assert sqlite_brin_count == 0


@pytest.mark.asyncio
async def test_adopt_legacy_schema_repairs_known_drift_and_upgrades(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    config_path = _write_sqlite_config(tmp_path / "legacy.yaml", database_path)
    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_config.attributes["bot_config_path"] = str(config_path)
    alembic_config.attributes["skip_logger_config"] = True
    await asyncio.to_thread(command.upgrade, alembic_config, "0001_initial")
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE alembic_version")
        connection.commit()

    await adopt_legacy_schema(str(config_path))

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        assert (
            await get_current_schema_revision(session) == get_expected_schema_revision()
        )
        column_rows = (
            await session.execute(text("PRAGMA table_info(frontier_states)"))
        ).mappings()
        columns = [row["name"] for row in column_rows]
    await engine.dispose()
    assert "extra" in columns


@pytest.mark.asyncio
async def test_adopt_legacy_schema_refuses_unknown_drift(tmp_path: Path) -> None:
    database_path = tmp_path / "invalid.sqlite3"
    config_path = _write_sqlite_config(tmp_path / "invalid.yaml", database_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE unexpected_table (id INTEGER)"))
    await engine.dispose()

    with pytest.raises(ValueError, match="Refusing legacy schema adoption"):
        await adopt_legacy_schema(str(config_path))


def _write_sqlite_config(path: Path, database_path: Path) -> Path:
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    path.write_text(f'database:\n  url: "{database_url}"\n', encoding="utf-8")
    return path


def _sqlite_table_exists(database_path: Path, table_name: str) -> bool:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
    return row is not None


def _sqlite_columns(database_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return {str(row[1]) for row in rows}


def _sqlite_indexes(database_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f'PRAGMA index_list("{table_name}")').fetchall()
    return {str(row[1]) for row in rows}
