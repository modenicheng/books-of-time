from dataclasses import fields
from datetime import timedelta

import pytest

from books_of_time.domain.cohort_policy import (
    CohortPolicy,
    CollectionTier,
    TierAssessment,
    TierSignals,
    apply_tier_assessment,
    desired_tier,
)


@pytest.mark.parametrize(
    "signals",
    [
        TierSignals(
            monitored_official=True, publish_age=timedelta(hours=5, minutes=59)
        ),
        TierSignals(active_event_core=True),
        TierSignals(major_creator_involved=True),
        TierSignals(pinned_tier=CollectionTier.S),
    ],
)
def test_objective_forced_signals_select_s(signals: TierSignals) -> None:
    assert desired_tier(signals, CohortPolicy.from_config(None)) is CollectionTier.S


def test_late_discovery_does_not_restart_official_s_window() -> None:
    signals = TierSignals(
        monitored_official=True,
        publish_age=timedelta(hours=8),
    )

    assert desired_tier(signals, CohortPolicy.from_config(None)) is CollectionTier.C


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        (TierSignals(view_growth_per_hour=6000), CollectionTier.S),
        (TierSignals(comment_growth_per_hour=60), CollectionTier.S),
        (
            TierSignals(
                hot_top20_turnover_ratio=0.35,
                hot_turnover_confirmations=2,
                hot_turnover_input_complete=True,
            ),
            CollectionTier.S,
        ),
        (TierSignals(view_growth_per_hour=1200), CollectionTier.A),
        (TierSignals(comment_growth_per_hour=20), CollectionTier.A),
        (
            TierSignals(
                hot_top20_turnover_ratio=0.20,
                hot_turnover_confirmations=2,
                hot_turnover_input_complete=True,
            ),
            CollectionTier.A,
        ),
        (TierSignals(view_growth_per_hour=300), CollectionTier.B),
        (TierSignals(comment_growth_per_hour=5), CollectionTier.B),
    ],
)
def test_each_numeric_signal_independently_selects_first_matching_tier(
    signals: TierSignals,
    expected: CollectionTier,
) -> None:
    assert desired_tier(signals, CohortPolicy.from_config(None)) is expected


@pytest.mark.parametrize(
    "signals",
    [
        TierSignals(
            hot_top20_turnover_ratio=0.9,
            hot_turnover_confirmations=1,
            hot_turnover_input_complete=True,
        ),
        TierSignals(
            hot_top20_turnover_ratio=0.9,
            hot_turnover_confirmations=2,
            hot_turnover_input_complete=False,
        ),
        TierSignals(hot_turnover_confirmations=2, hot_turnover_input_complete=True),
        TierSignals(),
    ],
)
def test_incomplete_or_absent_numeric_evidence_falls_through_to_c(
    signals: TierSignals,
) -> None:
    assert desired_tier(signals, CohortPolicy.from_config(None)) is CollectionTier.C


def test_tier_signals_exclude_model_derived_policy_inputs() -> None:
    field_names = {field.name for field in fields(TierSignals)}

    assert field_names.isdisjoint(
        {
            "bot_score",
            "automation_score",
            "steering_score",
            "stance_score",
            "coordination_score",
        }
    )


def test_upgrade_is_immediate_and_resets_downgrade_candidate() -> None:
    assessment = apply_tier_assessment(
        current_effective=CollectionTier.C,
        desired=CollectionTier.S,
        candidate_downgrade=CollectionTier.B,
        consecutive_count=1,
        policy=CohortPolicy.from_config(None),
    )

    assert assessment == TierAssessment(
        desired=CollectionTier.S,
        effective=CollectionTier.S,
        candidate_downgrade=None,
        consecutive_downgrade_count=0,
    )


def test_downgrade_requires_two_matching_assessments() -> None:
    policy = CohortPolicy.from_config(None)

    first = apply_tier_assessment(
        current_effective=CollectionTier.S,
        desired=CollectionTier.A,
        candidate_downgrade=None,
        consecutive_count=0,
        policy=policy,
    )
    second = apply_tier_assessment(
        current_effective=first.effective,
        desired=CollectionTier.A,
        candidate_downgrade=first.candidate_downgrade,
        consecutive_count=first.consecutive_downgrade_count,
        policy=policy,
    )

    assert first == TierAssessment(
        desired=CollectionTier.A,
        effective=CollectionTier.S,
        candidate_downgrade=CollectionTier.A,
        consecutive_downgrade_count=1,
    )
    assert second == TierAssessment(
        desired=CollectionTier.A,
        effective=CollectionTier.A,
        candidate_downgrade=None,
        consecutive_downgrade_count=0,
    )


def test_changed_downgrade_candidate_restarts_confirmation_count() -> None:
    assessment = apply_tier_assessment(
        current_effective=CollectionTier.S,
        desired=CollectionTier.B,
        candidate_downgrade=CollectionTier.A,
        consecutive_count=1,
        policy=CohortPolicy.from_config(None),
    )

    assert assessment.effective is CollectionTier.S
    assert assessment.candidate_downgrade is CollectionTier.B
    assert assessment.consecutive_downgrade_count == 1


def test_return_to_current_tier_resets_downgrade_candidate() -> None:
    assessment = apply_tier_assessment(
        current_effective=CollectionTier.S,
        desired=CollectionTier.S,
        candidate_downgrade=CollectionTier.A,
        consecutive_count=1,
        policy=CohortPolicy.from_config(None),
    )

    assert assessment == TierAssessment(
        desired=CollectionTier.S,
        effective=CollectionTier.S,
        candidate_downgrade=None,
        consecutive_downgrade_count=0,
    )
