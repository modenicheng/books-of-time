from datetime import time, timedelta
from pathlib import Path

import pytest

from books_of_time.config.loader import load_config
from books_of_time.domain.cohort_policy import (
    CohortComponentStatus,
    CohortPolicy,
    CohortRolloutMode,
    CohortStatus,
    CollectionTier,
    VideoLifeStage,
)


def test_cohort_policy_defaults_match_approved_contract() -> None:
    policy = CohortPolicy.from_config(None)

    assert policy.enabled is False
    assert policy.policy_version == "cohort-default-v2"
    assert policy.rollout_mode is CohortRolloutMode.SHADOW
    assert policy.planning_seconds == 30
    assert policy.timezone.key == "Asia/Shanghai"
    assert policy.checkpoint_hours == (6, 12, 18, 24)
    assert policy.checkpoint_max_lateness == timedelta(minutes=60)
    assert policy.downgrade_confirmations == 2
    assert policy.official_s_age == timedelta(hours=6)
    assert policy.hot_turnover_confirmations == 2
    assert policy.reassessment_interval == timedelta(minutes=60)
    assert policy.tier_intervals[CollectionTier.S].active == timedelta(minutes=2)
    assert policy.tier_intervals[CollectionTier.S].normal == timedelta(minutes=10)
    assert policy.tier_intervals[CollectionTier.A].normal == timedelta(minutes=30)
    assert policy.tier_intervals[CollectionTier.C].active == timedelta(minutes=60)
    assert policy.lifecycle.dormant_after == timedelta(days=7)
    assert policy.lifecycle.archive_after == timedelta(days=30)
    assert policy.lifecycle.dormant_interval == timedelta(days=1)
    assert policy.lifecycle.archived_metric_probe_interval == timedelta(days=7)
    assert policy.activity_windows[0].name == "lunch"
    assert policy.activity_windows[0].start == time(11, 30)
    assert policy.activity_windows[2].end == time(0, 30)
    assert policy.tier_thresholds[CollectionTier.S].view_growth_per_hour == 6000
    assert policy.tier_thresholds[CollectionTier.A].hot_top20_turnover_ratio == 0.20
    assert policy.tier_thresholds[CollectionTier.B].hot_top20_turnover_ratio is None
    assert policy.hot_comments.routine_pages == {
        CollectionTier.S: 3,
        CollectionTier.A: 2,
        CollectionTier.B: 1,
        CollectionTier.C: 1,
    }
    assert policy.hot_comments.checkpoint_pages[CollectionTier.S] == 20
    assert policy.hot_comments.max_pages_per_slice == 10
    assert policy.hot_comments.max_slice_seconds == 55

    persisted = policy.as_persisted_policy()
    assert persisted["timezone"] == "Asia/Shanghai"
    assert persisted["checkpoint_hours"] == [6, 12, 18, 24]
    assert persisted["tier_intervals_minutes"]["s"] == {
        "active": 2,
        "normal": 10,
    }
    assert persisted["activity_windows"]["defaults"][2] == {
        "name": "night",
        "start": "21:30",
        "end": "00:30",
    }
    assert persisted["hot_comments"]["checkpoint_pages"] == {
        "s": 20,
        "a": 10,
        "b": 3,
        "c": 1,
    }


def test_example_config_keeps_c3_shadow_planner_disabled() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config" / "config.yaml.example"

    policy = CohortPolicy.from_config(load_config(config_path, environ={}))

    assert policy.enabled is False
    assert policy.policy_version == "cohort-default-v2"
    assert policy.rollout_mode is CohortRolloutMode.SHADOW


def test_cohort_policy_enums_use_persisted_values() -> None:
    assert [tier.value for tier in CollectionTier] == ["s", "a", "b", "c"]
    assert [stage.value for stage in VideoLifeStage] == [
        "active",
        "dormant",
        "archived",
    ]
    assert CohortStatus.SHADOW_PLANNED.value == "shadow_planned"
    assert CohortStatus.NOT_APPLICABLE.value == "not_applicable"
    assert CohortComponentStatus.JOINED_ACTIVE_TASK.value == "joined_active_task"
    assert CohortComponentStatus.MISSED_DUE_TO_SERVICE_GAP.value == (
        "missed_due_to_service_gap"
    )


def test_cohort_policy_accepts_partial_overrides() -> None:
    policy = CohortPolicy.from_config(
        {
            "snapshot_cohorts": {
                "enabled": True,
                "policy_version": "cohort-experiment-v2",
                "rollout_mode": "live",
                "planning_seconds": 45,
                "checkpoint_hours": [3, 9],
                "tier_policy": {
                    "s": {"view_growth_per_hour": 9000},
                },
                "activity_windows": {
                    "defaults": [{"name": "late", "start": "23:00", "end": "01:00"}]
                },
            }
        }
    )

    assert policy.enabled is True
    assert policy.policy_version == "cohort-experiment-v2"
    assert policy.rollout_mode is CohortRolloutMode.LIVE
    assert policy.planning_seconds == 45
    assert policy.checkpoint_hours == (3, 9)
    assert policy.tier_thresholds[CollectionTier.S].view_growth_per_hour == 9000
    assert policy.tier_thresholds[CollectionTier.S].comment_growth_per_hour == 60
    assert policy.activity_windows[0].start == time(23, 0)


@pytest.mark.parametrize(
    ("snapshot_config", "message"),
    [
        (
            {"timezone": "Mars/Olympus_Mons"},
            "snapshot_cohorts.timezone must be a valid IANA timezone",
        ),
        (
            {"checkpoint_hours": [0, 6]},
            "snapshot_cohorts.checkpoint_hours must contain positive integers",
        ),
        (
            {"checkpoint_hours": [6, 6, 12]},
            "snapshot_cohorts.checkpoint_hours must be strictly increasing",
        ),
        (
            {"checkpoint_hours": [12, 6]},
            "snapshot_cohorts.checkpoint_hours must be strictly increasing",
        ),
        (
            {"checkpoint_hours": [True, 6]},
            "snapshot_cohorts.checkpoint_hours must contain positive integers",
        ),
        (
            {"tier_intervals_minutes": {"s": {"active": 0}}},
            "snapshot_cohorts.tier_intervals_minutes.s.active must be positive",
        ),
        (
            {"tier_intervals_minutes": {"a": {"normal": -1}}},
            "snapshot_cohorts.tier_intervals_minutes.a.normal must be positive",
        ),
        (
            {"tier_intervals_minutes": {"b": {"active": 90, "normal": 60}}},
            "snapshot_cohorts.tier_intervals_minutes.b.active must not exceed normal",
        ),
        (
            {"tier_intervals_minutes": {"invalid": {"active": 1, "normal": 1}}},
            "snapshot_cohorts.tier_intervals_minutes has unknown tier keys: invalid",
        ),
        (
            {"tier_policy": {"invalid": {"view_growth_per_hour": 1}}},
            "snapshot_cohorts.tier_policy has unknown keys: invalid",
        ),
        (
            {"tier_policy": {"s": {"view_growth_per_hour": 1000}}},
            "tier view growth thresholds must descend from s to a to b",
        ),
        (
            {"tier_policy": {"a": {"comment_growth_per_hour": 100}}},
            "tier comment growth thresholds must descend from s to a to b",
        ),
        (
            {"tier_policy": {"s": {"hot_top20_turnover_ratio": 1.1}}},
            "tier hot turnover ratios must be between 0 and 1",
        ),
        (
            {"tier_policy": {"a": {"hot_top20_turnover_ratio": -0.1}}},
            "tier hot turnover ratios must be between 0 and 1",
        ),
        (
            {
                "tier_policy": {
                    "s": {"hot_top20_turnover_ratio": 0.1},
                    "a": {"hot_top20_turnover_ratio": 0.2},
                }
            },
            "tier s hot turnover ratio must be at least tier a",
        ),
        (
            {"lifecycle": {"dormant_after_days": 30}},
            "lifecycle dormant_after_days must be less than archive_after_days",
        ),
        (
            {"lifecycle": {"archive_after_days": 0}},
            "snapshot_cohorts.lifecycle.archive_after_days must be positive",
        ),
        (
            {"downgrade_confirmations": 0},
            "snapshot_cohorts.downgrade_confirmations must be positive",
        ),
        (
            {"planning_seconds": True},
            "snapshot_cohorts.planning_seconds must be a positive integer",
        ),
        (
            {"policy_version": "  "},
            "snapshot_cohorts.policy_version must not be empty",
        ),
        (
            {"rollout_mode": 1},
            "snapshot_cohorts.rollout_mode must be 'shadow' or 'live'",
        ),
        (
            {"rollout_mode": "disabled"},
            "snapshot_cohorts.rollout_mode must be 'shadow' or 'live'",
        ),
        (
            {
                "activity_windows": {
                    "defaults": [{"name": "bad", "start": "9:00", "end": "10:00"}]
                }
            },
            "activity window start must use HH:MM",
        ),
        (
            {
                "activity_windows": {
                    "defaults": [{"name": "zero", "start": "10:00", "end": "10:00"}]
                }
            },
            "activity window start and end must differ",
        ),
    ],
)
def test_cohort_policy_rejects_invalid_configuration(
    snapshot_config: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        CohortPolicy.from_config({"snapshot_cohorts": snapshot_config})
