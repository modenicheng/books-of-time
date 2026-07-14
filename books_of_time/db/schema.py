from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from books_of_time.config import load_config
from books_of_time.db import models as _models  # noqa: F401
from books_of_time.db.base import Base
from books_of_time.domain.enums import BilibiliRequestType, TaskKind

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
_LEGACY_BASELINE_REVISION = "0001_initial"
_POST_BASELINE_TABLES = {
    "events",
    "event_targets",
    "event_videos",
    "event_keywords",
    "comment_analysis_flags",
    "request_budget_states",
    "operational_alert_states",
    "known_video_sources",
    "http_request_attempts",
    "collection_policy_versions",
    "video_collection_states",
    "snapshot_cohorts",
    "snapshot_cohort_components",
    "collection_schedule_gaps",
    "comment_scan_runs",
}
_POST_BASELINE_COLUMNS = {
    "collection_tasks": {
        "snapshot_cohort_id",
        "snapshot_cohort_component_id",
        "comment_scan_run_id",
        "scan_slice_no",
        "scan_slice_key",
    },
    "collection_coverage_stats": {
        "snapshot_cohort_id",
        "snapshot_cohort_component_id",
        "comment_scan_run_id",
    },
    "raw_page_observations": {"scan_run_id"},
    "comment_entities": {
        "platform_created_at",
        "author_level",
        "author_official_type",
        "author_official_description",
        "author_vip_status",
        "author_vip_type",
        "author_is_senior_member",
        "author_public_metadata_extra",
    },
    "comment_observations": {
        "scan_run_id",
        "platform_created_at",
        "author_level",
        "author_official_type",
        "author_official_description",
        "author_vip_status",
        "author_vip_type",
        "author_is_senior_member",
        "author_public_metadata_extra",
    },
}


async def create_schema(config_path: str | None = None) -> None:
    await _run_alembic(command.upgrade, "head", config_path=config_path)


async def adopt_legacy_schema(config_path: str | None = None) -> None:
    cfg = load_config(config_path)
    database_url = str(cfg["database"]["url"])
    schema = os.environ.get("BOT_DATABASE_SCHEMA")
    connect_args = (
        {"server_settings": {"search_path": schema}}
        if schema and make_url(database_url).get_backend_name() == "postgresql"
        else {}
    )
    engine = create_async_engine(database_url, connect_args=connect_args)
    try:
        async with engine.connect() as connection:
            has_revision = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).has_table(
                    "alembic_version"
                )
            )
            if has_revision:
                raise ValueError(
                    "Database already has alembic_version; use alembic upgrade head"
                )
            differences = await connection.run_sync(_schema_differences)
            unexpected = [
                difference
                for difference in differences
                if not _is_allowed_legacy_difference(difference)
            ]
            if unexpected:
                details = "; ".join(_describe_difference(item) for item in unexpected)
                raise ValueError(
                    "Refusing legacy schema adoption because unknown drift was found: "
                    f"{details}"
                )
            needs_frontier_extra = any(
                _is_frontier_extra_difference(difference) for difference in differences
            )
            dialect_name = connection.dialect.name

        if needs_frontier_extra:
            async with engine.begin() as connection:
                if dialect_name == "postgresql":
                    await connection.execute(
                        text(
                            "ALTER TABLE frontier_states ADD COLUMN extra "
                            "JSONB NOT NULL DEFAULT '{}'::jsonb"
                        )
                    )
                else:
                    await connection.execute(
                        text(
                            "ALTER TABLE frontier_states ADD COLUMN extra "
                            "JSON NOT NULL DEFAULT '{}'"
                        )
                    )

        if dialect_name == "postgresql":
            async with engine.connect() as connection:
                autocommit = await connection.execution_options(
                    isolation_level="AUTOCOMMIT"
                )
                for enum_name, values in (
                    ("taskkind", [kind.value for kind in TaskKind]),
                    (
                        "bilibilirequesttype",
                        [request_type.value for request_type in BilibiliRequestType],
                    ),
                ):
                    for value in values:
                        await autocommit.execute(
                            text(
                                f"ALTER TYPE {enum_name} "
                                f"ADD VALUE IF NOT EXISTS '{value}'"
                            )
                        )
    finally:
        await engine.dispose()

    await _run_alembic(
        command.stamp,
        _LEGACY_BASELINE_REVISION,
        config_path=config_path,
    )
    await _run_alembic(command.upgrade, "head", config_path=config_path)


def _schema_differences(sync_connection) -> list[Any]:
    def include_object(obj, _name, type_, _reflected, _compare_to) -> bool:
        return not (
            sync_connection.dialect.name != "postgresql"
            and type_ == "index"
            and obj.info.get("postgresql_only", False)
        )

    context = MigrationContext.configure(
        sync_connection,
        opts={"compare_type": True, "include_object": include_object},
    )
    return list(compare_metadata(context, Base.metadata))


def _is_allowed_legacy_difference(difference: Any) -> bool:
    if difference and isinstance(difference[0], (list, tuple)):
        return all(_is_allowed_legacy_difference(item) for item in difference)

    operation = difference[0]
    if operation == "add_table":
        return difference[1].name in _POST_BASELINE_TABLES
    if operation == "add_index":
        index = difference[1]
        return index.table.name in _POST_BASELINE_TABLES or _columns_are_post_baseline(
            index.table.name,
            {column.name for column in index.columns},
        )
    if operation == "add_fk":
        constraint = difference[1]
        return _columns_are_post_baseline(
            constraint.table.name,
            {element.parent.name for element in constraint.elements},
        )
    if operation == "add_column":
        return difference[3].name in _POST_BASELINE_COLUMNS.get(
            difference[2], set()
        ) or _is_frontier_extra_difference(difference)
    if operation == "modify_type":
        return difference[2:4] == ("scheduled_jobs", "job_kind")
    return _is_frontier_extra_difference(difference)


def _columns_are_post_baseline(table_name: str, column_names: set[str]) -> bool:
    allowed = _POST_BASELINE_COLUMNS.get(table_name, set())
    return bool(column_names) and column_names.issubset(allowed)


def _is_frontier_extra_difference(difference: Any) -> bool:
    return (
        difference[0] == "add_column"
        and difference[2] == "frontier_states"
        and difference[3].name == "extra"
    )


def _describe_difference(difference: Any) -> str:
    operation = str(difference[0])
    if operation in {"add_table", "remove_table"}:
        return f"{operation}:{difference[1].name}"
    if operation in {"add_column", "remove_column"}:
        return f"{operation}:{difference[2]}.{difference[3].name}"
    return operation


async def _run_alembic(
    operation,
    revision: str,
    *,
    config_path: str | None,
) -> None:
    alembic_config = Config(str(_ALEMBIC_INI))
    alembic_config.attributes["skip_logger_config"] = True
    if config_path is not None:
        alembic_config.attributes["bot_config_path"] = str(config_path)
    await asyncio.to_thread(operation, alembic_config, revision)
