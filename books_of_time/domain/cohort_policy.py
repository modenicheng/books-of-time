from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CollectionTier(StrEnum):
    S = "s"
    A = "a"
    B = "b"
    C = "c"


class VideoLifeStage(StrEnum):
    ACTIVE = "active"
    DORMANT = "dormant"
    ARCHIVED = "archived"


class CohortStatus(StrEnum):
    PLANNED = "planned"
    SHADOW_PLANNED = "shadow_planned"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    MISSED = "missed"
    CORRUPTED = "corrupted"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


class CohortComponentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    JOINED_ACTIVE_TASK = "joined_active_task"
    MISSED_DUE_TO_CAPACITY = "missed_due_to_capacity"
    MISSED_DUE_TO_SERVICE_GAP = "missed_due_to_service_gap"
    FAILED = "failed"
    CORRUPTED = "corrupted"
    NOT_APPLICABLE = "not_applicable"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class TierThreshold:
    view_growth_per_hour: int
    comment_growth_per_hour: int
    hot_top20_turnover_ratio: float | None = None


@dataclass(frozen=True)
class TierInterval:
    active: timedelta
    normal: timedelta


@dataclass(frozen=True)
class ActivityWindow:
    name: str
    start: time
    end: time


@dataclass(frozen=True)
class LifecyclePolicy:
    dormant_after: timedelta
    archive_after: timedelta
    dormant_interval: timedelta
    archived_metric_probe_interval: timedelta


@dataclass(frozen=True)
class TierSignals:
    monitored_official: bool = False
    publish_age: timedelta | None = None
    active_event_core: bool = False
    major_creator_involved: bool = False
    pinned_tier: CollectionTier | None = None
    view_growth_per_hour: int | None = None
    comment_growth_per_hour: int | None = None
    hot_top20_turnover_ratio: float | None = None
    hot_turnover_confirmations: int = 0
    hot_turnover_input_complete: bool = False


@dataclass(frozen=True)
class TierAssessment:
    desired: CollectionTier
    effective: CollectionTier
    candidate_downgrade: CollectionTier | None
    consecutive_downgrade_count: int


@dataclass(frozen=True)
class CohortPolicy:
    enabled: bool
    planning_seconds: int
    timezone: ZoneInfo
    checkpoint_hours: tuple[int, ...]
    checkpoint_max_lateness: timedelta
    downgrade_confirmations: int
    official_s_age: timedelta
    hot_turnover_confirmations: int
    reassessment_interval: timedelta
    tier_thresholds: Mapping[CollectionTier, TierThreshold]
    lifecycle: LifecyclePolicy
    activity_windows: tuple[ActivityWindow, ...]
    tier_intervals: Mapping[CollectionTier, TierInterval]

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> CohortPolicy:
        root = _mapping(config or {}, "config")
        section = _mapping(root.get("snapshot_cohorts", {}), "snapshot_cohorts")

        enabled = section.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("snapshot_cohorts.enabled must be a boolean")

        planning_seconds = _positive_int(
            section,
            "planning_seconds",
            30,
            "snapshot_cohorts.planning_seconds",
        )
        timezone = _timezone(section.get("timezone", "Asia/Shanghai"))
        checkpoint_hours = _checkpoint_hours(
            section.get("checkpoint_hours", (6, 12, 18, 24))
        )
        checkpoint_max_lateness = timedelta(
            minutes=_positive_int(
                section,
                "checkpoint_max_lateness_minutes",
                60,
                "snapshot_cohorts.checkpoint_max_lateness_minutes",
            )
        )
        downgrade_confirmations = _positive_int(
            section,
            "downgrade_confirmations",
            2,
            "snapshot_cohorts.downgrade_confirmations",
            message="snapshot_cohorts.downgrade_confirmations must be positive",
        )

        tier_policy = _mapping(
            section.get("tier_policy", {}),
            "snapshot_cohorts.tier_policy",
        )
        official_s_age = timedelta(
            hours=_positive_int(
                tier_policy,
                "official_s_age_hours",
                6,
                "snapshot_cohorts.tier_policy.official_s_age_hours",
            )
        )
        hot_turnover_confirmations = _positive_int(
            tier_policy,
            "hot_turnover_confirmations",
            2,
            "snapshot_cohorts.tier_policy.hot_turnover_confirmations",
        )
        reassessment_interval = timedelta(
            minutes=_positive_int(
                tier_policy,
                "reassess_after_24h_minutes",
                60,
                "snapshot_cohorts.tier_policy.reassess_after_24h_minutes",
            )
        )
        tier_thresholds = _tier_thresholds(tier_policy)
        lifecycle = _lifecycle(section.get("lifecycle", {}))
        activity_windows = _activity_windows(section.get("activity_windows", {}))
        tier_intervals = _tier_intervals(section.get("tier_intervals_minutes", {}))

        return cls(
            enabled=enabled,
            planning_seconds=planning_seconds,
            timezone=timezone,
            checkpoint_hours=checkpoint_hours,
            checkpoint_max_lateness=checkpoint_max_lateness,
            downgrade_confirmations=downgrade_confirmations,
            official_s_age=official_s_age,
            hot_turnover_confirmations=hot_turnover_confirmations,
            reassessment_interval=reassessment_interval,
            tier_thresholds=MappingProxyType(tier_thresholds),
            lifecycle=lifecycle,
            activity_windows=activity_windows,
            tier_intervals=MappingProxyType(tier_intervals),
        )


def is_activity_window(now: datetime, policy: CohortPolicy) -> bool:
    _require_aware(now)
    local_time = now.astimezone(policy.timezone).time()
    return any(
        _time_in_window(local_time, window) for window in policy.activity_windows
    )


def age_growth_interval(
    anchor: datetime,
    now: datetime,
    recent_view_growth_last_hour: int | None,
) -> timedelta:
    _require_aware(anchor)
    _require_aware(now)
    age = max(now - anchor, timedelta())
    if age < timedelta(minutes=30):
        return timedelta(minutes=1)
    if age < timedelta(hours=6):
        return timedelta(minutes=5)

    growth = recent_view_growth_last_hour or 0
    if growth > 30_000:
        return timedelta(minutes=5)
    if growth > 6_000:
        return timedelta(minutes=15)
    if growth > 1_200:
        return timedelta(minutes=30)
    return timedelta(minutes=120)


def effective_interval(
    anchor: datetime,
    now: datetime,
    *,
    tier: CollectionTier,
    policy: CohortPolicy,
    recent_view_growth_last_hour: int | None = None,
    next_checkpoint_at: datetime | None = None,
) -> timedelta:
    age_interval = age_growth_interval(
        anchor,
        now,
        recent_view_growth_last_hour,
    )
    tier_interval = policy.tier_intervals[tier]
    ceiling = (
        tier_interval.active
        if is_activity_window(now, policy)
        else tier_interval.normal
    )
    candidates = [age_interval, ceiling]
    if next_checkpoint_at is not None:
        _require_aware(next_checkpoint_at)
        candidates.append(max(next_checkpoint_at - now, timedelta()))
    return min(candidates)


def next_aligned_slot(
    anchor: datetime,
    now: datetime,
    interval: timedelta,
) -> datetime:
    _require_aware(anchor)
    _require_aware(now)
    if interval <= timedelta():
        raise ValueError("interval must be positive")
    if now < anchor:
        return anchor
    elapsed_slots = (now - anchor) // interval
    return anchor + interval * (elapsed_slots + 1)


def checkpoint_times(
    anchor: datetime,
    policy: CohortPolicy,
) -> tuple[tuple[int, datetime], ...]:
    _require_aware(anchor)
    return tuple(
        (hours, anchor + timedelta(hours=hours)) for hours in policy.checkpoint_hours
    )


def routine_cohort_key(bvid: str, scheduled_for: datetime) -> str:
    return f"snapshot:{bvid}:{_canonical_utc_second(scheduled_for)}:routine"


def checkpoint_cohort_key(bvid: str, hours: int) -> str:
    _require_positive_hours(hours)
    return f"snapshot:{bvid}:age:{hours}h"


def recovery_cohort_key(bvid: str, latest_overdue_hours: int) -> str:
    _require_positive_hours(latest_overdue_hours)
    return f"snapshot:{bvid}:recovery:through:{latest_overdue_hours}h"


def component_key(cohort_key: str, component_kind: str) -> str:
    return f"{cohort_key}:{component_kind}"


def desired_tier(signals: TierSignals, policy: CohortPolicy) -> CollectionTier:
    official_initial_s = (
        signals.monitored_official
        and signals.publish_age is not None
        and timedelta() <= signals.publish_age < policy.official_s_age
    )
    if (
        official_initial_s
        or signals.active_event_core
        or signals.major_creator_involved
        or signals.pinned_tier is CollectionTier.S
    ):
        return CollectionTier.S
    if signals.pinned_tier is not None:
        return signals.pinned_tier

    turnover_eligible = (
        signals.hot_turnover_input_complete
        and signals.hot_turnover_confirmations >= policy.hot_turnover_confirmations
        and signals.hot_top20_turnover_ratio is not None
    )
    for tier in (CollectionTier.S, CollectionTier.A, CollectionTier.B):
        threshold = policy.tier_thresholds[tier]
        if (
            _meets_threshold(
                signals.view_growth_per_hour,
                threshold.view_growth_per_hour,
            )
            or _meets_threshold(
                signals.comment_growth_per_hour,
                threshold.comment_growth_per_hour,
            )
            or (
                turnover_eligible
                and threshold.hot_top20_turnover_ratio is not None
                and signals.hot_top20_turnover_ratio
                >= threshold.hot_top20_turnover_ratio
            )
        ):
            return tier
    return CollectionTier.C


def apply_tier_assessment(
    *,
    current_effective: CollectionTier,
    desired: CollectionTier,
    candidate_downgrade: CollectionTier | None,
    consecutive_count: int,
    policy: CohortPolicy,
) -> TierAssessment:
    if consecutive_count < 0:
        raise ValueError("consecutive_count must be non-negative")

    current_rank = _TIER_RANK[current_effective]
    desired_rank = _TIER_RANK[desired]
    if desired_rank <= current_rank:
        return TierAssessment(
            desired=desired,
            effective=desired if desired_rank < current_rank else current_effective,
            candidate_downgrade=None,
            consecutive_downgrade_count=0,
        )

    next_count = consecutive_count + 1 if candidate_downgrade is desired else 1
    if next_count >= policy.downgrade_confirmations:
        return TierAssessment(
            desired=desired,
            effective=desired,
            candidate_downgrade=None,
            consecutive_downgrade_count=0,
        )
    return TierAssessment(
        desired=desired,
        effective=current_effective,
        candidate_downgrade=desired,
        consecutive_downgrade_count=next_count,
    )


_TIER_THRESHOLD_DEFAULTS = {
    CollectionTier.S: (6000, 60, 0.35),
    CollectionTier.A: (1200, 20, 0.20),
    CollectionTier.B: (300, 5, None),
}

_TIER_INTERVAL_DEFAULTS = {
    CollectionTier.S: (2, 10),
    CollectionTier.A: (10, 30),
    CollectionTier.B: (30, 60),
    CollectionTier.C: (60, 120),
}

_TIER_RANK = {
    CollectionTier.S: 0,
    CollectionTier.A: 1,
    CollectionTier.B: 2,
    CollectionTier.C: 3,
}

_ACTIVITY_WINDOW_DEFAULTS = (
    {"name": "lunch", "start": "11:30", "end": "13:30"},
    {"name": "dinner", "start": "17:30", "end": "20:30"},
    {"name": "night", "start": "21:30", "end": "00:30"},
)

_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


def _time_in_window(value: time, window: ActivityWindow) -> bool:
    if window.start < window.end:
        return window.start <= value < window.end
    return value >= window.start or value < window.end


def _canonical_utc_second(value: datetime) -> str:
    _require_aware(value)
    normalized = value.astimezone(UTC).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_positive_hours(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("hours must be a positive integer")


def _meets_threshold(value: int | None, threshold: int) -> bool:
    return value is not None and value >= threshold


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")
    return value


def _positive_int(
    mapping: Mapping[str, Any],
    key: str,
    default: int,
    path: str,
    *,
    message: str | None = None,
) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(message or f"{path} must be a positive integer")
    return value


def _non_negative_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{path} must be a non-negative integer")
    return value


def _timezone(value: object) -> ZoneInfo:
    if not isinstance(value, str) or not value:
        raise ValueError("snapshot_cohorts.timezone must be a valid IANA timezone")
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(
            "snapshot_cohorts.timezone must be a valid IANA timezone"
        ) from exc


def _checkpoint_hours(value: object) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(
            "snapshot_cohorts.checkpoint_hours must contain positive integers"
        )
    hours = tuple(value)
    if not hours or any(
        isinstance(hour, bool) or not isinstance(hour, int) or hour <= 0
        for hour in hours
    ):
        raise ValueError(
            "snapshot_cohorts.checkpoint_hours must contain positive integers"
        )
    if any(left >= right for left, right in pairwise(hours)):
        raise ValueError(
            "snapshot_cohorts.checkpoint_hours must be strictly increasing"
        )
    return hours


def _tier_thresholds(
    tier_policy: Mapping[str, Any],
) -> dict[CollectionTier, TierThreshold]:
    thresholds: dict[CollectionTier, TierThreshold] = {}
    for tier, defaults in _TIER_THRESHOLD_DEFAULTS.items():
        values = _mapping(
            tier_policy.get(tier.value, {}),
            f"snapshot_cohorts.tier_policy.{tier.value}",
        )
        view_default, comment_default, turnover_default = defaults
        view_growth = _non_negative_int(
            values.get("view_growth_per_hour", view_default),
            f"snapshot_cohorts.tier_policy.{tier.value}.view_growth_per_hour",
        )
        comment_growth = _non_negative_int(
            values.get("comment_growth_per_hour", comment_default),
            f"snapshot_cohorts.tier_policy.{tier.value}.comment_growth_per_hour",
        )
        turnover = _optional_ratio(
            values.get("hot_top20_turnover_ratio", turnover_default)
        )
        thresholds[tier] = TierThreshold(
            view_growth_per_hour=view_growth,
            comment_growth_per_hour=comment_growth,
            hot_top20_turnover_ratio=turnover,
        )

    ordered = tuple(thresholds[tier] for tier in _TIER_THRESHOLD_DEFAULTS)
    if not _descending(item.view_growth_per_hour for item in ordered):
        raise ValueError("tier view growth thresholds must descend from s to a to b")
    if not _descending(item.comment_growth_per_hour for item in ordered):
        raise ValueError("tier comment growth thresholds must descend from s to a to b")
    s_turnover = thresholds[CollectionTier.S].hot_top20_turnover_ratio
    a_turnover = thresholds[CollectionTier.A].hot_top20_turnover_ratio
    if s_turnover is None or a_turnover is None or s_turnover < a_turnover:
        raise ValueError("tier s hot turnover ratio must be at least tier a")
    return thresholds


def _optional_ratio(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("tier hot turnover ratios must be between 0 and 1")
    ratio = float(value)
    if not 0 <= ratio <= 1:
        raise ValueError("tier hot turnover ratios must be between 0 and 1")
    return ratio


def _descending(values: Iterable[int]) -> bool:
    materialized = tuple(values)
    return all(left >= right for left, right in pairwise(materialized))


def _lifecycle(value: object) -> LifecyclePolicy:
    lifecycle = _mapping(value, "snapshot_cohorts.lifecycle")
    dormant_days = _positive_int(
        lifecycle,
        "dormant_after_days",
        7,
        "snapshot_cohorts.lifecycle.dormant_after_days",
        message="snapshot_cohorts.lifecycle.dormant_after_days must be positive",
    )
    archive_days = _positive_int(
        lifecycle,
        "archive_after_days",
        30,
        "snapshot_cohorts.lifecycle.archive_after_days",
        message="snapshot_cohorts.lifecycle.archive_after_days must be positive",
    )
    if dormant_days >= archive_days:
        raise ValueError(
            "lifecycle dormant_after_days must be less than archive_after_days"
        )
    return LifecyclePolicy(
        dormant_after=timedelta(days=dormant_days),
        archive_after=timedelta(days=archive_days),
        dormant_interval=timedelta(
            minutes=_positive_int(
                lifecycle,
                "dormant_interval_minutes",
                1440,
                "snapshot_cohorts.lifecycle.dormant_interval_minutes",
                message=(
                    "snapshot_cohorts.lifecycle.dormant_interval_minutes "
                    "must be positive"
                ),
            )
        ),
        archived_metric_probe_interval=timedelta(
            minutes=_positive_int(
                lifecycle,
                "archived_metric_probe_minutes",
                10080,
                "snapshot_cohorts.lifecycle.archived_metric_probe_minutes",
                message=(
                    "snapshot_cohorts.lifecycle.archived_metric_probe_minutes "
                    "must be positive"
                ),
            )
        ),
    )


def _activity_windows(value: object) -> tuple[ActivityWindow, ...]:
    activity = _mapping(value, "snapshot_cohorts.activity_windows")
    raw_windows = activity.get("defaults", _ACTIVITY_WINDOW_DEFAULTS)
    if isinstance(raw_windows, (str, bytes)) or not isinstance(raw_windows, Sequence):
        raise ValueError("snapshot_cohorts.activity_windows.defaults must be a list")

    windows: list[ActivityWindow] = []
    names: set[str] = set()
    for raw_window in raw_windows:
        window = _mapping(raw_window, "activity window")
        name = window.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("activity window name must be a non-empty string")
        if name in names:
            raise ValueError("activity window names must be unique")
        start = _clock_time(window.get("start"), "start")
        end = _clock_time(window.get("end"), "end")
        if start == end:
            raise ValueError("activity window start and end must differ")
        names.add(name)
        windows.append(ActivityWindow(name=name, start=start, end=end))
    return tuple(windows)


def _clock_time(value: object, boundary: str) -> time:
    if not isinstance(value, str) or _TIME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"activity window {boundary} must use HH:MM")
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def _tier_intervals(value: object) -> dict[CollectionTier, TierInterval]:
    intervals = _mapping(value, "snapshot_cohorts.tier_intervals_minutes")
    result: dict[CollectionTier, TierInterval] = {}
    for tier, (active_default, normal_default) in _TIER_INTERVAL_DEFAULTS.items():
        tier_values = _mapping(
            intervals.get(tier.value, {}),
            f"snapshot_cohorts.tier_intervals_minutes.{tier.value}",
        )
        active = _positive_int(
            tier_values,
            "active",
            active_default,
            f"snapshot_cohorts.tier_intervals_minutes.{tier.value}.active",
            message=(
                "snapshot_cohorts.tier_intervals_minutes."
                f"{tier.value}.active must be positive"
            ),
        )
        normal = _positive_int(
            tier_values,
            "normal",
            normal_default,
            f"snapshot_cohorts.tier_intervals_minutes.{tier.value}.normal",
            message=(
                "snapshot_cohorts.tier_intervals_minutes."
                f"{tier.value}.normal must be positive"
            ),
        )
        if active > normal:
            raise ValueError(
                "snapshot_cohorts.tier_intervals_minutes."
                f"{tier.value}.active must not exceed normal"
            )
        result[tier] = TierInterval(
            active=timedelta(minutes=active),
            normal=timedelta(minutes=normal),
        )
    return result
