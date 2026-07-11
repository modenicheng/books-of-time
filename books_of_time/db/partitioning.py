from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class MonthPartition:
    parent_table: str
    name: str
    start: datetime
    end: datetime

    def create_sql(self) -> str:
        return (
            f"CREATE TABLE IF NOT EXISTS {self.name} "
            f"PARTITION OF {self.parent_table} FOR VALUES FROM "
            f"('{self.start.isoformat()}') TO ('{self.end.isoformat()}')"
        )


def comment_observation_partition_for(value: datetime) -> MonthPartition:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("partition timestamp must include a timezone offset")
    utc = value.astimezone(UTC)
    start = datetime(utc.year, utc.month, 1, tzinfo=UTC)
    end = _add_months(start, 1)
    return MonthPartition(
        parent_table="comment_observations",
        name=f"comment_observations_y{start.year:04d}m{start.month:02d}",
        start=start,
        end=end,
    )


def iter_comment_observation_partitions(
    value: datetime,
    *,
    months_ahead: int,
) -> tuple[MonthPartition, ...]:
    if not 0 <= months_ahead <= 120:
        raise ValueError("months_ahead must be between 0 and 120")
    first = comment_observation_partition_for(value)
    return tuple(
        comment_observation_partition_for(_add_months(first.start, offset))
        for offset in range(months_ahead + 1)
    )


def _add_months(value: datetime, count: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 + count
    year, zero_based_month = divmod(month_index, 12)
    return datetime(year, zero_based_month + 1, 1, tzinfo=UTC)
