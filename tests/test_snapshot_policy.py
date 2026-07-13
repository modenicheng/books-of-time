from datetime import UTC, datetime, timedelta

from books_of_time.task_orchestrator.snapshot_policy import (
    get_next_snapshot_at,
    get_next_snapshot_interval,
)


def test_snapshot_interval_uses_one_minute_for_first_thirty_minutes() -> None:
    published_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    now = published_at + timedelta(minutes=4, seconds=20)

    assert get_next_snapshot_interval(published_at, now) == timedelta(minutes=1)


def test_snapshot_interval_uses_five_minutes_until_six_hours() -> None:
    published_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    now = published_at + timedelta(hours=2, minutes=10)

    assert get_next_snapshot_interval(published_at, now) == timedelta(minutes=5)


def test_snapshot_interval_adapts_after_six_hours_from_recent_view_growth() -> None:
    published_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    now = published_at + timedelta(hours=7)

    assert get_next_snapshot_interval(
        published_at, now, recent_view_growth_last_hour=31_000
    ) == timedelta(minutes=5)
    assert get_next_snapshot_interval(
        published_at, now, recent_view_growth_last_hour=9_000
    ) == timedelta(minutes=15)
    assert get_next_snapshot_interval(
        published_at, now, recent_view_growth_last_hour=2_000
    ) == timedelta(minutes=30)
    assert get_next_snapshot_interval(
        published_at, now, recent_view_growth_last_hour=500
    ) == timedelta(minutes=120)


def test_next_snapshot_uses_absolute_publish_time_axis() -> None:
    published_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    assert get_next_snapshot_at(
        published_at,
        published_at + timedelta(minutes=4, seconds=20),
    ) == published_at + timedelta(minutes=5)

    assert get_next_snapshot_at(
        published_at,
        published_at + timedelta(minutes=31, seconds=1),
    ) == published_at + timedelta(minutes=35)


def test_next_snapshot_continues_after_discovery_window_closes() -> None:
    published_at = datetime(2026, 7, 8, 13, 0, tzinfo=UTC)
    after_stop = datetime(2026, 7, 8, 14, 30, tzinfo=UTC)  # 22:30 in Shanghai

    assert get_next_snapshot_at(published_at, after_stop) == datetime(
        2026,
        7,
        8,
        14,
        35,
        tzinfo=UTC,
    )
