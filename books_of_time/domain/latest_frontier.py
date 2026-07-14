from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

MAX_FRONTIER_ANCHORS = 5


class AnchorComment(Protocol):
    rpid: int
    platform_created_at: datetime | None


def anchors_from_comments(
    comments: Sequence[AnchorComment],
) -> tuple[dict[str, object], ...]:
    anchors: list[dict[str, object]] = []
    seen_rpids: set[int] = set()
    for comment in comments:
        rpid = _positive_rpid(comment.rpid)
        if rpid in seen_rpids:
            continue
        anchors.append(
            {
                "rpid": rpid,
                "platform_created_at": _timestamp_text(
                    comment.platform_created_at,
                ),
            }
        )
        seen_rpids.add(rpid)
        if len(anchors) == MAX_FRONTIER_ANCHORS:
            break
    return tuple(anchors)


def normalize_anchor_set(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("frontier anchors must be a list or tuple")
    if len(value) > MAX_FRONTIER_ANCHORS:
        raise ValueError(
            f"frontier anchors must contain at most {MAX_FRONTIER_ANCHORS}"
        )

    normalized: list[dict[str, object]] = []
    seen_rpids: set[int] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "rpid",
            "platform_created_at",
        }:
            raise ValueError("frontier anchor has an invalid shape")
        rpid = _positive_rpid(item["rpid"])
        if rpid in seen_rpids:
            raise ValueError("frontier anchor RPIDs must be unique")
        normalized.append(
            {
                "rpid": rpid,
                "platform_created_at": _normalized_timestamp_text(
                    item["platform_created_at"]
                ),
            }
        )
        seen_rpids.add(rpid)
    return tuple(normalized)


def anchor_rpids(anchors: object) -> frozenset[int]:
    return frozenset(int(item["rpid"]) for item in normalize_anchor_set(anchors))


def primary_anchor(anchors: object) -> tuple[int | None, datetime | None]:
    normalized = normalize_anchor_set(anchors)
    if not normalized:
        return None, None
    timestamp = normalized[0]["platform_created_at"]
    return (
        int(normalized[0]["rpid"]),
        datetime.fromisoformat(str(timestamp)).astimezone(UTC)
        if timestamp is not None
        else None,
    )


def page_matches_anchor(
    comments: Sequence[AnchorComment],
    anchors: object,
) -> bool:
    retained = anchor_rpids(anchors)
    return bool(retained) and any(comment.rpid in retained for comment in comments)


def latest_slice_seconds(effective_interval_seconds: float | int | None) -> int:
    if effective_interval_seconds is None:
        return 55
    if isinstance(effective_interval_seconds, bool):
        raise ValueError("effective_interval_seconds must be positive")
    value = float(effective_interval_seconds)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("effective_interval_seconds must be positive")
    return min(55, max(10, math.floor(value * 0.4)))


def _positive_rpid(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("frontier anchor rpid must be a positive integer")
    return value


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("frontier anchor timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _normalized_timestamp_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("frontier anchor timestamp must be an ISO string or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("frontier anchor timestamp is invalid") from exc
    return _timestamp_text(parsed)
