from __future__ import annotations

import pytest

from books_of_time.domain.cohort_policy import (
    CohortPolicy,
    CollectionTier,
    HotPagePlan,
    hot_page_plan,
)


@pytest.mark.parametrize(
    ("tier", "routine", "checkpoint"),
    [
        (CollectionTier.S, HotPagePlan(1, 3, 4, 0, 3), HotPagePlan(1, 3, 4, 17, 20)),
        (CollectionTier.A, HotPagePlan(1, 2, 3, 0, 2), HotPagePlan(1, 2, 3, 8, 10)),
        (CollectionTier.B, HotPagePlan(1, 1, 2, 0, 1), HotPagePlan(1, 1, 2, 2, 3)),
        (CollectionTier.C, HotPagePlan(1, 1, 2, 0, 1), HotPagePlan(1, 1, 2, 0, 1)),
    ],
)
def test_hot_page_plan_matches_tier_targets(
    tier: CollectionTier,
    routine: HotPagePlan,
    checkpoint: HotPagePlan,
) -> None:
    policy = CohortPolicy.from_config(None)

    assert hot_page_plan(policy, tier, include_deep=False) == routine
    assert hot_page_plan(policy, tier, include_deep=True) == checkpoint


def test_dormant_hot_page_plan_is_always_one_core_page() -> None:
    policy = CohortPolicy.from_config(None)

    assert hot_page_plan(
        policy,
        CollectionTier.S,
        include_deep=True,
        dormant=True,
    ) == HotPagePlan(1, 1, 2, 0, 1)


def test_hot_policy_supports_partial_tier_overrides_and_persists_them() -> None:
    policy = CohortPolicy.from_config(
        {
            "scheduler": {"lease_seconds": 90},
            "snapshot_cohorts": {
                "policy_version": "cohort-hot-test-v1",
                "hot_comments": {
                    "routine_pages": {"s": 4},
                    "checkpoint_pages": {"s": 24},
                    "max_pages_per_slice": 8,
                    "max_slice_seconds": 45,
                },
            },
        }
    )

    assert policy.hot_comments.routine_pages[CollectionTier.S] == 4
    assert policy.hot_comments.routine_pages[CollectionTier.A] == 2
    assert policy.hot_comments.checkpoint_pages[CollectionTier.S] == 24
    assert policy.hot_comments.max_pages_per_slice == 8
    assert policy.hot_comments.max_slice_seconds == 45
    assert policy.as_persisted_policy()["hot_comments"] == {
        "routine_pages": {"s": 4, "a": 2, "b": 1, "c": 1},
        "checkpoint_pages": {"s": 24, "a": 10, "b": 3, "c": 1},
        "max_pages_per_slice": 8,
        "max_slice_seconds": 45,
    }


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            {"hot_comments": {"unknown": 1}},
            "snapshot_cohorts.hot_comments has unknown keys: unknown",
        ),
        (
            {"hot_comments": {"routine_pages": {"invalid": 1}}},
            "snapshot_cohorts.hot_comments.routine_pages has unknown tier keys: invalid",
        ),
        (
            {"hot_comments": {"routine_pages": {"s": True}}},
            "snapshot_cohorts.hot_comments.routine_pages.s must be positive",
        ),
        (
            {
                "hot_comments": {
                    "routine_pages": {"s": 4},
                    "checkpoint_pages": {"s": 3},
                }
            },
            (
                "snapshot_cohorts.hot_comments.checkpoint_pages.s must be at least "
                "routine_pages.s"
            ),
        ),
        (
            {"hot_comments": {"max_pages_per_slice": 0}},
            "snapshot_cohorts.hot_comments.max_pages_per_slice must be positive",
        ),
        (
            {"hot_comments": {"max_slice_seconds": 0}},
            "snapshot_cohorts.hot_comments.max_slice_seconds must be positive",
        ),
        (
            {
                "scheduler": {"lease_seconds": 55},
                "hot_comments": {"max_slice_seconds": 55},
            },
            (
                "snapshot_cohorts.hot_comments.max_slice_seconds must be less than "
                "scheduler.lease_seconds"
            ),
        ),
    ],
)
def test_hot_policy_rejects_invalid_configuration(
    config: dict[str, object],
    message: str,
) -> None:
    scheduler = config.pop("scheduler", None)
    root: dict[str, object] = {"snapshot_cohorts": config}
    if scheduler is not None:
        root["scheduler"] = scheduler

    with pytest.raises(ValueError, match=f"^{message}$"):
        CohortPolicy.from_config(root)
