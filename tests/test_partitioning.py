from datetime import UTC, datetime
from pathlib import Path

from books_of_time.db.partitioning import (
    comment_observation_partition_for,
    iter_comment_observation_partitions,
)


def test_comment_observation_month_partition_handles_year_boundary() -> None:
    partition = comment_observation_partition_for(
        datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    )

    assert partition.name == "comment_observations_y2026m12"
    assert partition.start == datetime(2026, 12, 1, tzinfo=UTC)
    assert partition.end == datetime(2027, 1, 1, tzinfo=UTC)
    assert partition.create_sql() == (
        "CREATE TABLE IF NOT EXISTS comment_observations_y2026m12 "
        "PARTITION OF comment_observations_v2 FOR VALUES FROM "
        "('2026-12-01T00:00:00+00:00') TO ('2027-01-01T00:00:00+00:00')"
    )


def test_partition_plan_creates_current_and_future_months() -> None:
    partitions = iter_comment_observation_partitions(
        datetime(2026, 11, 15, tzinfo=UTC),
        months_ahead=3,
    )

    assert [partition.name for partition in partitions] == [
        "comment_observations_y2026m11",
        "comment_observations_y2026m12",
        "comment_observations_y2027m01",
        "comment_observations_y2027m02",
    ]


def test_partition_design_records_safe_migration_contract() -> None:
    design = (
        Path(__file__).resolve().parents[1] / "docs" / "PARTITIONING.md"
    ).read_text(encoding="utf-8")

    for required in (
        "PRIMARY KEY (captured_at, id)",
        "comment_observations_v2",
        "dual-write",
        "DEFAULT partition",
        "rollback",
        "comment_observation_media",
        "comment_visibility_events",
    ):
        assert required in design
