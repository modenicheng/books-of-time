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


class CohortRolloutMode(StrEnum):
    SHADOW = "shadow"
    LIVE = "live"


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
class HotCommentPolicy:
    routine_pages: Mapping[CollectionTier, int]
    checkpoint_pages: Mapping[CollectionTier, int]
    max_pages_per_slice: int
    max_slice_seconds: int


@dataclass(frozen=True)
class HotPagePlan:
    core_start_page: int
    core_pages: int
    deep_start_page: int
    deep_pages: int
    total_pages: int


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
class ComponentOutcome:
    status: CohortComponentStatus
    required: bool
    started: bool


@dataclass(frozen=True)
class CohortPolicy:
    enabled: bool
    policy_version: str
    rollout_mode: CohortRolloutMode
    planning_seconds: int
    timezone: ZoneInfo
    checkpoint_hours: tuple[int, ...]
    checkpoint_max_lateness: timedelta
    downgrade_confirmations: int
    official_s_age: timedelta
    hot_turnover_confirmations: int
    reassessment_interval: timedelta
    tier_thresholds: Mapping[CollectionTier, TierThreshold]
    hot_comments: HotCommentPolicy
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

        policy_version_value = section.get("policy_version", "cohort-default-v2")
        if (
            not isinstance(policy_version_value, str)
            or not policy_version_value.strip()
        ):
            raise ValueError("snapshot_cohorts.policy_version must not be empty")
        policy_version = policy_version_value.strip()

        rollout_mode_value = section.get("rollout_mode", CohortRolloutMode.SHADOW.value)
        try:
            if not isinstance(rollout_mode_value, str):
                raise ValueError
            rollout_mode = CohortRolloutMode(rollout_mode_value.strip().casefold())
        except ValueError as exc:
            raise ValueError(
                "snapshot_cohorts.rollout_mode must be 'shadow' or 'live'"
            ) from exc

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
        unknown_tier_policy_keys = _unknown_keys(
            tier_policy,
            {
                "official_s_age_hours",
                "reassess_after_24h_minutes",
                "hot_turnover_confirmations",
                "s",
                "a",
                "b",
            },
        )
        if unknown_tier_policy_keys:
            raise ValueError(
                "snapshot_cohorts.tier_policy has unknown keys: "
                + ", ".join(unknown_tier_policy_keys)
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
        scheduler = _mapping(root.get("scheduler", {}), "scheduler")
        lease_seconds = _positive_int(
            scheduler,
            "lease_seconds",
            120,
            "scheduler.lease_seconds",
        )
        hot_comments = _hot_comment_policy(
            section.get("hot_comments", {}),
            lease_seconds=lease_seconds,
        )
        lifecycle = _lifecycle(section.get("lifecycle", {}))
        activity_windows = _activity_windows(section.get("activity_windows", {}))
        tier_intervals = _tier_intervals(section.get("tier_intervals_minutes", {}))

        return cls(
            enabled=enabled,
            policy_version=policy_version,
            rollout_mode=rollout_mode,
            planning_seconds=planning_seconds,
            timezone=timezone,
            checkpoint_hours=checkpoint_hours,
            checkpoint_max_lateness=checkpoint_max_lateness,
            downgrade_confirmations=downgrade_confirmations,
            official_s_age=official_s_age,
            hot_turnover_confirmations=hot_turnover_confirmations,
            reassessment_interval=reassessment_interval,
            tier_thresholds=MappingProxyType(tier_thresholds),
            hot_comments=hot_comments,
            lifecycle=lifecycle,
            activity_windows=activity_windows,
            tier_intervals=MappingProxyType(tier_intervals),
        )

    def as_persisted_policy(self) -> dict[str, Any]:
        return {
            "planning_seconds": self.planning_seconds,
            "timezone": self.timezone.key,
            "checkpoint_hours": list(self.checkpoint_hours),
            "checkpoint_max_lateness_minutes": int(
                self.checkpoint_max_lateness.total_seconds() // 60
            ),
            "downgrade_confirmations": self.downgrade_confirmations,
            "tier_policy": {
                "official_s_age_hours": int(
                    self.official_s_age.total_seconds() // 3600
                ),
                "reassess_after_24h_minutes": int(
                    self.reassessment_interval.total_seconds() // 60
                ),
                "hot_turnover_confirmations": self.hot_turnover_confirmations,
                **{
                    tier.value: {
                        "view_growth_per_hour": threshold.view_growth_per_hour,
                        "comment_growth_per_hour": threshold.comment_growth_per_hour,
                        **(
                            {
                                "hot_top20_turnover_ratio": (
                                    threshold.hot_top20_turnover_ratio
                                )
                            }
                            if threshold.hot_top20_turnover_ratio is not None
                            else {}
                        ),
                    }
                    for tier, threshold in self.tier_thresholds.items()
                },
            },
            "hot_comments": {
                "routine_pages": {
                    tier.value: pages
                    for tier, pages in self.hot_comments.routine_pages.items()
                },
                "checkpoint_pages": {
                    tier.value: pages
                    for tier, pages in self.hot_comments.checkpoint_pages.items()
                },
                "max_pages_per_slice": self.hot_comments.max_pages_per_slice,
                "max_slice_seconds": self.hot_comments.max_slice_seconds,
            },
            "lifecycle": {
                "dormant_after_days": int(
                    self.lifecycle.dormant_after.total_seconds() // 86400
                ),
                "archive_after_days": int(
                    self.lifecycle.archive_after.total_seconds() // 86400
                ),
                "dormant_interval_minutes": int(
                    self.lifecycle.dormant_interval.total_seconds() // 60
                ),
                "archived_metric_probe_minutes": int(
                    self.lifecycle.archived_metric_probe_interval.total_seconds() // 60
                ),
            },
            "activity_windows": {
                "defaults": [
                    {
                        "name": window.name,
                        "start": window.start.strftime("%H:%M"),
                        "end": window.end.strftime("%H:%M"),
                    }
                    for window in self.activity_windows
                ]
            },
            "tier_intervals_minutes": {
                tier.value: {
                    "active": int(interval.active.total_seconds() // 60),
                    "normal": int(interval.normal.total_seconds() // 60),
                }
                for tier, interval in self.tier_intervals.items()
            },
        }


def hot_page_plan(
    policy: CohortPolicy,
    tier: CollectionTier,
    *,
    include_deep: bool,
    dormant: bool = False,
) -> HotPagePlan:
    core_pages = 1 if dormant else policy.hot_comments.routine_pages[tier]
    total_pages = (
        core_pages
        if dormant or not include_deep
        else policy.hot_comments.checkpoint_pages[tier]
    )
    return HotPagePlan(
        core_start_page=1,
        core_pages=core_pages,
        deep_start_page=core_pages + 1,
        deep_pages=total_pages - core_pages,
        total_pages=total_pages,
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


def determine_life_stage(
    publish_age: timedelta,
    *,
    low_growth_evidence: bool | None,
    policy: CohortPolicy,
    active_event: bool = False,
    operator_pinned: bool = False,
    renewed_growth: bool = False,
) -> VideoLifeStage:
    if active_event or operator_pinned or renewed_growth:
        return VideoLifeStage.ACTIVE
    if low_growth_evidence is not True:
        return VideoLifeStage.ACTIVE
    if publish_age >= policy.lifecycle.archive_after:
        return VideoLifeStage.ARCHIVED
    if publish_age >= policy.lifecycle.dormant_after:
        return VideoLifeStage.DORMANT
    return VideoLifeStage.ACTIVE


def component_kinds_for_stage(
    stage: VideoLifeStage,
    *,
    frontier_complete: bool,
) -> tuple[str, ...]:
    if stage is VideoLifeStage.ARCHIVED:
        return ("video_metrics",)
    components = ("video_metrics", "hot_core")
    if stage is VideoLifeStage.ACTIVE or frontier_complete:
        return (*components, "latest_current_head")
    return components


def aggregate_cohort_status(
    outcomes: Sequence[ComponentOutcome],
) -> CohortStatus:
    required = tuple(outcome for outcome in outcomes if outcome.required)
    if not required:
        raise ValueError("at least one required component is needed")

    statuses = tuple(outcome.status for outcome in required)
    if CohortComponentStatus.CORRUPTED in statuses:
        return CohortStatus.CORRUPTED
    if any(status in _ACTIVE_COMPONENT_STATUSES for status in statuses):
        return CohortStatus.RUNNING
    if all(status is CohortComponentStatus.PENDING for status in statuses):
        return CohortStatus.PLANNED
    if all(status is CohortComponentStatus.NOT_APPLICABLE for status in statuses):
        return CohortStatus.NOT_APPLICABLE

    applicable = tuple(
        outcome
        for outcome in required
        if outcome.status is not CohortComponentStatus.NOT_APPLICABLE
    )
    any_started = any(_component_started(outcome) for outcome in required)
    if (
        not any_started
        and applicable
        and all(
            outcome.status is CohortComponentStatus.BLOCKED for outcome in applicable
        )
    ):
        return CohortStatus.BLOCKED
    if not any_started and any(
        outcome.status in _MISSED_COMPONENT_STATUSES for outcome in applicable
    ):
        return CohortStatus.MISSED
    if all(
        status in {CohortComponentStatus.COMPLETE, CohortComponentStatus.NOT_APPLICABLE}
        for status in statuses
    ):
        return CohortStatus.COMPLETE
    if any_started and CohortComponentStatus.PENDING in statuses:
        return CohortStatus.RUNNING
    if not any_started and all(
        status
        in {
            CohortComponentStatus.PENDING,
            CohortComponentStatus.BLOCKED,
            CohortComponentStatus.NOT_APPLICABLE,
        }
        for status in statuses
    ):
        return CohortStatus.PLANNED
    return CohortStatus.PARTIAL


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

_HOT_ROUTINE_PAGE_DEFAULTS = {
    CollectionTier.S: 3,
    CollectionTier.A: 2,
    CollectionTier.B: 1,
    CollectionTier.C: 1,
}

_HOT_CHECKPOINT_PAGE_DEFAULTS = {
    CollectionTier.S: 20,
    CollectionTier.A: 10,
    CollectionTier.B: 3,
    CollectionTier.C: 1,
}

_TIER_RANK = {
    CollectionTier.S: 0,
    CollectionTier.A: 1,
    CollectionTier.B: 2,
    CollectionTier.C: 3,
}

_ACTIVE_COMPONENT_STATUSES = {
    CohortComponentStatus.RUNNING,
    CohortComponentStatus.JOINED_ACTIVE_TASK,
}

_MISSED_COMPONENT_STATUSES = {
    CohortComponentStatus.MISSED_DUE_TO_CAPACITY,
    CohortComponentStatus.MISSED_DUE_TO_SERVICE_GAP,
}

_IMPLICITLY_STARTED_COMPONENT_STATUSES = {
    CohortComponentStatus.RUNNING,
    CohortComponentStatus.COMPLETE,
    CohortComponentStatus.PARTIAL,
    CohortComponentStatus.JOINED_ACTIVE_TASK,
    CohortComponentStatus.FAILED,
    CohortComponentStatus.CORRUPTED,
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


def _component_started(outcome: ComponentOutcome) -> bool:
    return outcome.started or outcome.status in _IMPLICITLY_STARTED_COMPONENT_STATUSES


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")
    return value


def _unknown_keys(values: Mapping[str, Any], allowed: set[str]) -> list[str]:
    return sorted(str(key) for key in values if key not in allowed)


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
    unknown_tier_keys = _unknown_keys(
        intervals,
        {tier.value for tier in _TIER_INTERVAL_DEFAULTS},
    )
    if unknown_tier_keys:
        raise ValueError(
            "snapshot_cohorts.tier_intervals_minutes has unknown tier keys: "
            + ", ".join(unknown_tier_keys)
        )
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


def _hot_comment_policy(
    value: object,
    *,
    lease_seconds: int,
) -> HotCommentPolicy:
    hot_comments = _mapping(value, "snapshot_cohorts.hot_comments")
    unknown_keys = _unknown_keys(
        hot_comments,
        {
            "routine_pages",
            "checkpoint_pages",
            "max_pages_per_slice",
            "max_slice_seconds",
        },
    )
    if unknown_keys:
        raise ValueError(
            "snapshot_cohorts.hot_comments has unknown keys: " + ", ".join(unknown_keys)
        )

    routine_pages = _tier_page_counts(
        hot_comments.get("routine_pages", {}),
        path="snapshot_cohorts.hot_comments.routine_pages",
        defaults=_HOT_ROUTINE_PAGE_DEFAULTS,
    )
    checkpoint_pages = _tier_page_counts(
        hot_comments.get("checkpoint_pages", {}),
        path="snapshot_cohorts.hot_comments.checkpoint_pages",
        defaults=_HOT_CHECKPOINT_PAGE_DEFAULTS,
    )
    for tier in CollectionTier:
        if checkpoint_pages[tier] < routine_pages[tier]:
            raise ValueError(
                "snapshot_cohorts.hot_comments.checkpoint_pages."
                f"{tier.value} must be at least routine_pages.{tier.value}"
            )

    max_pages_per_slice = _positive_int(
        hot_comments,
        "max_pages_per_slice",
        10,
        "snapshot_cohorts.hot_comments.max_pages_per_slice",
        message="snapshot_cohorts.hot_comments.max_pages_per_slice must be positive",
    )
    max_slice_seconds = _positive_int(
        hot_comments,
        "max_slice_seconds",
        55,
        "snapshot_cohorts.hot_comments.max_slice_seconds",
        message="snapshot_cohorts.hot_comments.max_slice_seconds must be positive",
    )
    if max_slice_seconds >= lease_seconds:
        raise ValueError(
            "snapshot_cohorts.hot_comments.max_slice_seconds must be less than "
            "scheduler.lease_seconds"
        )
    return HotCommentPolicy(
        routine_pages=MappingProxyType(routine_pages),
        checkpoint_pages=MappingProxyType(checkpoint_pages),
        max_pages_per_slice=max_pages_per_slice,
        max_slice_seconds=max_slice_seconds,
    )


def _tier_page_counts(
    value: object,
    *,
    path: str,
    defaults: Mapping[CollectionTier, int],
) -> dict[CollectionTier, int]:
    values = _mapping(value, path)
    unknown_tiers = _unknown_keys(values, {tier.value for tier in CollectionTier})
    if unknown_tiers:
        raise ValueError(f"{path} has unknown tier keys: " + ", ".join(unknown_tiers))
    return {
        tier: _positive_int(
            values,
            tier.value,
            defaults[tier],
            f"{path}.{tier.value}",
            message=f"{path}.{tier.value} must be positive",
        )
        for tier in CollectionTier
    }
