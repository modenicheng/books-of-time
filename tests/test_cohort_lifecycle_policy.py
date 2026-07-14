from datetime import timedelta

import pytest

from books_of_time.domain.cohort_policy import (
    CohortComponentStatus,
    CohortPolicy,
    CohortStatus,
    ComponentOutcome,
    VideoLifeStage,
    aggregate_cohort_status,
    component_kinds_for_stage,
    determine_life_stage,
)


def test_lifecycle_is_active_before_dormant_age() -> None:
    assert (
        determine_life_stage(
            timedelta(days=6, hours=23),
            low_growth_evidence=True,
            policy=CohortPolicy.from_config(None),
        )
        is VideoLifeStage.ACTIVE
    )


def test_lifecycle_enters_dormant_and_archived_at_exact_boundaries() -> None:
    policy = CohortPolicy.from_config(None)

    assert (
        determine_life_stage(
            timedelta(days=7),
            low_growth_evidence=True,
            policy=policy,
        )
        is VideoLifeStage.DORMANT
    )
    assert (
        determine_life_stage(
            timedelta(days=30),
            low_growth_evidence=True,
            policy=policy,
        )
        is VideoLifeStage.ARCHIVED
    )


@pytest.mark.parametrize(
    "reactivation",
    [
        {"active_event": True},
        {"operator_pinned": True},
        {"renewed_growth": True},
    ],
)
def test_event_pin_or_renewed_growth_reactivates_old_video(
    reactivation: dict[str, bool],
) -> None:
    assert (
        determine_life_stage(
            timedelta(days=90),
            low_growth_evidence=True,
            policy=CohortPolicy.from_config(None),
            **reactivation,
        )
        is VideoLifeStage.ACTIVE
    )


@pytest.mark.parametrize("low_growth_evidence", [None, False])
def test_missing_or_contrary_growth_evidence_does_not_archive(
    low_growth_evidence: bool | None,
) -> None:
    assert (
        determine_life_stage(
            timedelta(days=90),
            low_growth_evidence=low_growth_evidence,
            policy=CohortPolicy.from_config(None),
        )
        is VideoLifeStage.ACTIVE
    )


@pytest.mark.parametrize(
    ("stage", "frontier_complete", "expected"),
    [
        (
            VideoLifeStage.ACTIVE,
            False,
            ("video_metrics", "hot_core", "latest_current_head"),
        ),
        (
            VideoLifeStage.DORMANT,
            False,
            ("video_metrics", "hot_core"),
        ),
        (
            VideoLifeStage.DORMANT,
            True,
            ("video_metrics", "hot_core", "latest_current_head"),
        ),
        (VideoLifeStage.ARCHIVED, True, ("video_metrics",)),
    ],
)
def test_component_eligibility_depends_on_lifecycle_and_frontier(
    stage: VideoLifeStage,
    frontier_complete: bool,
    expected: tuple[str, ...],
) -> None:
    assert (
        component_kinds_for_stage(
            stage,
            frontier_complete=frontier_complete,
        )
        == expected
    )


def outcome(
    status: CohortComponentStatus,
    *,
    required: bool = True,
    started: bool = False,
) -> ComponentOutcome:
    return ComponentOutcome(status=status, required=required, started=started)


def test_corrupted_required_component_wins_over_running() -> None:
    assert (
        aggregate_cohort_status(
            [
                outcome(CohortComponentStatus.RUNNING, started=True),
                outcome(CohortComponentStatus.CORRUPTED, started=True),
            ]
        )
        is CohortStatus.CORRUPTED
    )


@pytest.mark.parametrize(
    "active_status",
    [CohortComponentStatus.RUNNING, CohortComponentStatus.JOINED_ACTIVE_TASK],
)
def test_running_or_joined_component_makes_cohort_running(
    active_status: CohortComponentStatus,
) -> None:
    assert (
        aggregate_cohort_status(
            [
                outcome(CohortComponentStatus.COMPLETE, started=True),
                outcome(active_status, started=True),
            ]
        )
        is CohortStatus.RUNNING
    )


def test_all_pending_components_are_planned() -> None:
    assert (
        aggregate_cohort_status(
            [
                outcome(CohortComponentStatus.PENDING),
                outcome(CohortComponentStatus.PENDING),
            ]
        )
        is CohortStatus.PLANNED
    )


def test_unstarted_all_applicable_blocked_components_are_blocked() -> None:
    assert (
        aggregate_cohort_status(
            [
                outcome(CohortComponentStatus.BLOCKED),
                outcome(CohortComponentStatus.BLOCKED),
                outcome(CohortComponentStatus.NOT_APPLICABLE),
            ]
        )
        is CohortStatus.BLOCKED
    )


@pytest.mark.parametrize(
    "miss_status",
    [
        CohortComponentStatus.MISSED_DUE_TO_CAPACITY,
        CohortComponentStatus.MISSED_DUE_TO_SERVICE_GAP,
    ],
)
def test_unstarted_terminal_miss_makes_cohort_missed(
    miss_status: CohortComponentStatus,
) -> None:
    assert (
        aggregate_cohort_status(
            [
                outcome(miss_status),
                outcome(CohortComponentStatus.BLOCKED),
            ]
        )
        is CohortStatus.MISSED
    )


def test_mixed_complete_and_incomplete_terminal_components_are_partial() -> None:
    assert (
        aggregate_cohort_status(
            [
                outcome(CohortComponentStatus.COMPLETE, started=True),
                outcome(CohortComponentStatus.FAILED, started=True),
            ]
        )
        is CohortStatus.PARTIAL
    )


def test_pending_component_after_another_started_keeps_cohort_running() -> None:
    assert (
        aggregate_cohort_status(
            [
                outcome(CohortComponentStatus.COMPLETE, started=True),
                outcome(CohortComponentStatus.PENDING),
            ]
        )
        is CohortStatus.RUNNING
    )


def test_complete_and_not_applicable_required_components_are_complete() -> None:
    assert (
        aggregate_cohort_status(
            [
                outcome(CohortComponentStatus.COMPLETE, started=True),
                outcome(CohortComponentStatus.NOT_APPLICABLE),
            ]
        )
        is CohortStatus.COMPLETE
    )


def test_every_required_component_not_applicable_is_not_applicable() -> None:
    assert (
        aggregate_cohort_status(
            [
                outcome(CohortComponentStatus.NOT_APPLICABLE),
                outcome(CohortComponentStatus.NOT_APPLICABLE),
            ]
        )
        is CohortStatus.NOT_APPLICABLE
    )


def test_optional_component_does_not_block_required_completion() -> None:
    assert (
        aggregate_cohort_status(
            [
                outcome(CohortComponentStatus.COMPLETE, started=True),
                outcome(
                    CohortComponentStatus.CORRUPTED,
                    required=False,
                    started=True,
                ),
            ]
        )
        is CohortStatus.COMPLETE
    )


def test_empty_required_component_set_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one required component is needed"):
        aggregate_cohort_status(
            [outcome(CohortComponentStatus.COMPLETE, required=False, started=True)]
        )
