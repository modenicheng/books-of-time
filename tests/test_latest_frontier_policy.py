from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from books_of_time.domain.latest_frontier import (
    anchor_rpids,
    anchors_from_comments,
    latest_slice_seconds,
    normalize_anchor_set,
    page_matches_anchor,
    primary_anchor,
)
from books_of_time.parsers.comments import ParsedComment


def _comment(
    rpid: int,
    *,
    platform_created_at: datetime | None = None,
) -> ParsedComment:
    return ParsedComment(
        rpid=rpid,
        oid=9001,
        bvid="BV-LATEST-FRONTIER",
        root_rpid=None,
        parent_rpid=None,
        author_mid=rpid + 1000,
        author_name=f"user-{rpid}",
        content=f"comment-{rpid}",
        content_hash=bytes([rpid % 256]) * 32,
        like_count=0,
        reply_count=0,
        position=1,
        platform_created_at=platform_created_at,
    )


def test_frontier_anchors_preserve_head_order_and_limit() -> None:
    times = [datetime(2026, 7, 14, 8, minute, tzinfo=UTC) for minute in range(5)]
    comments = [
        _comment(101, platform_created_at=times[0]),
        _comment(102),
        _comment(103, platform_created_at=times[2]),
        _comment(104, platform_created_at=times[3]),
        _comment(105, platform_created_at=times[4]),
        _comment(106, platform_created_at=times[4] + timedelta(minutes=1)),
    ]

    anchors = anchors_from_comments(comments)

    assert [item["rpid"] for item in anchors] == [101, 102, 103, 104, 105]
    assert anchors[0]["platform_created_at"] == times[0].isoformat()
    assert anchors[1]["platform_created_at"] is None
    assert anchor_rpids(anchors) == frozenset({101, 102, 103, 104, 105})
    assert primary_anchor(anchors) == (101, times[0])
    assert page_matches_anchor([_comment(105)], anchors) is True
    assert page_matches_anchor([_comment(106)], anchors) is False


def test_empty_frontier_is_explicit_and_valid() -> None:
    assert anchors_from_comments([]) == ()
    assert normalize_anchor_set([]) == ()
    assert anchor_rpids(()) == frozenset()
    assert primary_anchor(()) == (None, None)
    assert page_matches_anchor([_comment(101)], ()) is False


def test_normalize_anchor_set_converts_aware_timestamps_to_utc() -> None:
    normalized = normalize_anchor_set(
        [
            {
                "rpid": 101,
                "platform_created_at": "2026-07-14T16:00:00+08:00",
            },
            {"rpid": 102, "platform_created_at": None},
        ]
    )

    assert normalized == (
        {
            "rpid": 101,
            "platform_created_at": "2026-07-14T08:00:00+00:00",
        },
        {"rpid": 102, "platform_created_at": None},
    )
    assert primary_anchor(normalized) == (
        101,
        datetime(2026, 7, 14, 8, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        [{"rpid": 0, "platform_created_at": None}],
        [{"rpid": True, "platform_created_at": None}],
        [{"rpid": 101}],
        [{"rpid": 101, "platform_created_at": "not-a-time"}],
        [{"rpid": 101, "platform_created_at": "2026-07-14T08:00:00"}],
        [
            {"rpid": 101, "platform_created_at": None},
            {"rpid": 101, "platform_created_at": None},
        ],
        [{"rpid": rpid, "platform_created_at": None} for rpid in range(101, 107)],
    ],
)
def test_normalize_anchor_set_rejects_invalid_evidence(value: object) -> None:
    with pytest.raises(ValueError):
        normalize_anchor_set(value)


@pytest.mark.parametrize(
    ("effective_interval_seconds", "expected"),
    [
        (None, 55),
        (1, 10),
        (60, 24),
        (120, 48),
        (137, 54),
        (138, 55),
        (600, 55),
    ],
)
def test_latest_slice_seconds_is_bounded_by_cadence(
    effective_interval_seconds: float | int | None,
    expected: int,
) -> None:
    assert latest_slice_seconds(effective_interval_seconds) == expected


@pytest.mark.parametrize("value", [True, False, 0, -1, -0.5, float("nan")])
def test_latest_slice_seconds_rejects_invalid_intervals(value: object) -> None:
    with pytest.raises(ValueError):
        latest_slice_seconds(value)  # type: ignore[arg-type]
