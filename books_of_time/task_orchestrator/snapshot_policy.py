from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import floor
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class CoreWindow:
    start_hour: int = 10
    stop_hour: int = 22
    timezone_name: str = "Asia/Shanghai"

    def allows_detail_polling(self, at: datetime) -> bool:
        local = at.astimezone(ZoneInfo(self.timezone_name))
        return self.start_hour <= local.hour < self.stop_hour


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
    core_window: CoreWindow | None = None,
) -> datetime | None:
    window = core_window or CoreWindow()
    if not window.allows_detail_polling(now):
        return None

    interval = get_next_snapshot_interval(
        published_at,
        now,
        recent_view_growth_last_hour=recent_view_growth_last_hour,
    )
    age_seconds = max((now - published_at).total_seconds(), 0)
    interval_seconds = interval.total_seconds()
    next_slot = floor(age_seconds / interval_seconds) + 1
    return published_at + (interval * next_slot)
