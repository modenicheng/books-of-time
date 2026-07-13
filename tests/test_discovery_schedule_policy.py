from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from books_of_time.task_orchestrator.discovery_schedule_policy import (
    DiscoverySchedulePolicy,
)


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 7, 13, 1, 59, tzinfo=UTC), False),
        (datetime(2026, 7, 13, 2, 0, tzinfo=UTC), True),
        (datetime(2026, 7, 13, 13, 59, tzinfo=UTC), True),
        (datetime(2026, 7, 13, 14, 0, tzinfo=UTC), False),
    ],
)
def test_discovery_window_uses_shanghai_inclusive_start_exclusive_stop(
    at: datetime,
    expected: bool,
) -> None:
    policy = DiscoverySchedulePolicy()

    assert policy.allows_discovery(at) is expected


@pytest.mark.parametrize(
    "focus_time",
    ["11:00", "12:00", "13:00", "18:00", "19:00", "19:30", "20:00"],
)
def test_discovery_policy_recognizes_each_focus_minute(focus_time: str) -> None:
    local_hour, local_minute = (int(part) for part in focus_time.split(":"))
    at = datetime(
        2026,
        7,
        13,
        local_hour - 8,
        local_minute,
        45,
        tzinfo=UTC,
    )

    assert DiscoverySchedulePolicy().focus_time_for(at) == focus_time


def test_discovery_policy_does_not_mark_adjacent_minute_as_focus() -> None:
    at = datetime(2026, 7, 13, 3, 1, tzinfo=UTC)  # 11:01 Asia/Shanghai

    assert DiscoverySchedulePolicy().focus_time_for(at) is None


def test_discovery_policy_returns_focus_slot_at_exact_local_minute() -> None:
    at = datetime(2026, 7, 13, 3, 0, 45, tzinfo=UTC)

    assert DiscoverySchedulePolicy().focus_slot_for(at) == datetime(
        2026,
        7,
        13,
        11,
        0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )


@pytest.mark.parametrize(
    "focus_times",
    [
        ("11",),
        ("24:00",),
        ("09:59",),
        ("22:00",),
        ("11:00", "11:00"),
    ],
)
def test_discovery_policy_rejects_invalid_focus_configuration(
    focus_times: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        DiscoverySchedulePolicy(focus_times=focus_times)
