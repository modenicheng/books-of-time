from __future__ import annotations

from datetime import datetime, timedelta
from math import floor


def get_next_snapshot_interval(
    published_at: datetime,
    now: datetime,
    *,
    recent_view_growth_last_hour: int | None = None,
) -> timedelta:
    age = max(now - published_at, timedelta())

    if age < timedelta(minutes=30):
        return timedelta(minutes=1)
    if age < timedelta(hours=6):
        return timedelta(minutes=5)

    growth = recent_view_growth_last_hour or 0
    avg_growth_per_minute = growth / 60
    if avg_growth_per_minute > 500:
        return timedelta(minutes=5)
    if avg_growth_per_minute > 100:
        return timedelta(minutes=15)
    if avg_growth_per_minute > 20:
        return timedelta(minutes=30)
    return timedelta(minutes=120)


def get_next_snapshot_at(
    published_at: datetime,
    now: datetime,
    *,
    recent_view_growth_last_hour: int | None = None,
) -> datetime:
    interval = get_next_snapshot_interval(
        published_at,
        now,
        recent_view_growth_last_hour=recent_view_growth_last_hour,
    )
    age_seconds = max((now - published_at).total_seconds(), 0)
    interval_seconds = interval.total_seconds()
    next_slot = floor(age_seconds / interval_seconds) + 1
    return published_at + (interval * next_slot)
