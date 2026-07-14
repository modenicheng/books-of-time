from datetime import UTC, datetime, timedelta, timezone

import pytest

from books_of_time.domain.cohort_policy import (
    CohortPolicy,
    CollectionTier,
    age_growth_interval,
    checkpoint_cohort_key,
    checkpoint_times,
    component_key,
    effective_interval,
    is_activity_window,
    next_aligned_slot,
    recovery_cohort_key,
    routine_cohort_key,
)


def test_activity_windows_use_configured_timezone_and_half_open_boundaries() -> None:
    policy = CohortPolicy.from_config(None)

    assert is_activity_window(datetime(2026, 7, 14, 3, 29, tzinfo=UTC), policy) is False
    assert is_activity_window(datetime(2026, 7, 14, 3, 30, tzinfo=UTC), policy) is True
    assert is_activity_window(datetime(2026, 7, 14, 5, 29, tzinfo=UTC), policy) is True
    assert is_activity_window(datetime(2026, 7, 14, 5, 30, tzinfo=UTC), policy) is False
    assert is_activity_window(datetime(2026, 7, 14, 9, 30, tzinfo=UTC), policy) is True
    assert (
        is_activity_window(datetime(2026, 7, 14, 12, 30, tzinfo=UTC), policy) is False
    )


def test_cross_midnight_activity_window_is_start_inclusive_end_exclusive() -> None:
    policy = CohortPolicy.from_config(None)

    assert is_activity_window(datetime(2026, 7, 14, 13, 30, tzinfo=UTC), policy) is True
    assert is_activity_window(datetime(2026, 7, 14, 16, 29, tzinfo=UTC), policy) is True
    assert (
        is_activity_window(datetime(2026, 7, 14, 16, 30, tzinfo=UTC), policy) is False
    )


def test_overlapping_activity_windows_return_one_boolean_state() -> None:
    policy = CohortPolicy.from_config(
        {
            "snapshot_cohorts": {
                "activity_windows": {
                    "defaults": [
                        {"name": "first", "start": "11:00", "end": "13:00"},
                        {"name": "second", "start": "12:00", "end": "14:00"},
                    ]
                }
            }
        }
    )

    assert is_activity_window(datetime(2026, 7, 14, 4, 30, tzinfo=UTC), policy) is True


@pytest.mark.parametrize(
    ("age", "growth", "expected"),
    [
        (timedelta(minutes=29, seconds=59), None, timedelta(minutes=1)),
        (timedelta(minutes=30), None, timedelta(minutes=5)),
        (timedelta(hours=5, minutes=59, seconds=59), None, timedelta(minutes=5)),
        (timedelta(hours=6), 30_001, timedelta(minutes=5)),
        (timedelta(hours=6), 30_000, timedelta(minutes=15)),
        (timedelta(hours=6), 6_001, timedelta(minutes=15)),
        (timedelta(hours=6), 6_000, timedelta(minutes=30)),
        (timedelta(hours=6), 1_201, timedelta(minutes=30)),
        (timedelta(hours=6), 1_200, timedelta(minutes=120)),
        (timedelta(hours=6), None, timedelta(minutes=120)),
    ],
)
def test_age_growth_interval_preserves_existing_thresholds(
    age: timedelta,
    growth: int | None,
    expected: timedelta,
) -> None:
    anchor = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)

    assert age_growth_interval(anchor, anchor + age, growth) == expected


def test_effective_interval_uses_age_tier_and_activity_minimum() -> None:
    policy = CohortPolicy.from_config(None)
    anchor = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)

    assert effective_interval(
        anchor,
        anchor + timedelta(hours=2, minutes=30),  # 11:30 Asia/Shanghai
        tier=CollectionTier.S,
        policy=policy,
    ) == timedelta(minutes=2)
    assert effective_interval(
        anchor,
        anchor + timedelta(hours=2),  # 11:00 Asia/Shanghai
        tier=CollectionTier.S,
        policy=policy,
    ) == timedelta(minutes=5)
    assert effective_interval(
        anchor,
        anchor + timedelta(hours=10, minutes=30),  # 19:30 Asia/Shanghai
        tier=CollectionTier.A,
        policy=policy,
        recent_view_growth_last_hour=0,
    ) == timedelta(minutes=10)
    assert effective_interval(
        anchor,
        anchor + timedelta(hours=7),  # 16:00 Asia/Shanghai
        tier=CollectionTier.C,
        policy=policy,
        recent_view_growth_last_hour=0,
    ) == timedelta(minutes=120)


def test_effective_interval_is_limited_by_next_checkpoint() -> None:
    policy = CohortPolicy.from_config(None)
    anchor = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    now = anchor + timedelta(hours=5)

    assert effective_interval(
        anchor,
        now,
        tier=CollectionTier.C,
        policy=policy,
        next_checkpoint_at=now + timedelta(seconds=90),
    ) == timedelta(seconds=90)
    assert (
        effective_interval(
            anchor,
            now,
            tier=CollectionTier.C,
            policy=policy,
            next_checkpoint_at=now,
        )
        == timedelta()
    )


def test_next_slot_remains_aligned_to_immutable_publish_anchor() -> None:
    anchor = datetime(2026, 7, 14, 0, 2, 17, tzinfo=UTC)

    assert next_aligned_slot(
        anchor,
        datetime(2026, 7, 14, 0, 19, tzinfo=UTC),
        timedelta(minutes=5),
    ) == datetime(2026, 7, 14, 0, 22, 17, tzinfo=UTC)
    assert next_aligned_slot(
        anchor,
        datetime(2026, 7, 14, 0, 22, 17, tzinfo=UTC),
        timedelta(minutes=5),
    ) == datetime(2026, 7, 14, 0, 27, 17, tzinfo=UTC)


def test_checkpoint_times_are_publish_age_offsets() -> None:
    policy = CohortPolicy.from_config(None)
    anchor = datetime(2026, 7, 14, 0, 2, 17, tzinfo=UTC)

    assert checkpoint_times(anchor, policy) == (
        (6, anchor + timedelta(hours=6)),
        (12, anchor + timedelta(hours=12)),
        (18, anchor + timedelta(hours=18)),
        (24, anchor + timedelta(hours=24)),
    )


def test_stable_keys_use_canonical_whole_second_utc() -> None:
    china = timezone(timedelta(hours=8))
    scheduled = datetime(2026, 7, 14, 11, 30, 45, 987654, tzinfo=china)

    assert routine_cohort_key("BV-KEY", scheduled) == (
        "snapshot:BV-KEY:2026-07-14T03:30:45Z:routine"
    )
    assert routine_cohort_key(
        "BV-KEY",
        scheduled.replace(microsecond=1),
    ) == routine_cohort_key("BV-KEY", scheduled)
    assert checkpoint_cohort_key("BV-KEY", 6) == "snapshot:BV-KEY:age:6h"
    assert recovery_cohort_key("BV-KEY", 18) == ("snapshot:BV-KEY:recovery:through:18h")
    assert (
        component_key(
            "snapshot:BV-KEY:age:6h",
            "video_metrics",
        )
        == "snapshot:BV-KEY:age:6h:video_metrics"
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda policy: is_activity_window(datetime(2026, 7, 14, 3, 30), policy),
        lambda policy: age_growth_interval(
            datetime(2026, 7, 14, 0, 0),
            datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
            None,
        ),
        lambda policy: effective_interval(
            datetime(2026, 7, 14, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
            tier=CollectionTier.C,
            policy=policy,
            next_checkpoint_at=datetime(2026, 7, 14, 2, 0),
        ),
        lambda policy: routine_cohort_key("BV-KEY", datetime(2026, 7, 14, 0, 0)),
    ],
)
def test_time_policy_rejects_naive_datetimes(call) -> None:
    with pytest.raises(ValueError, match="datetime must be timezone-aware"):
        call(CohortPolicy.from_config(None))
