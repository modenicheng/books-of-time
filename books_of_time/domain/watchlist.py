from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WatchlistPolicy:
    hot_max_position: int = 3
    reply_growth_min: int = 5
    like_growth_min: int = 20
    controversy_keywords: tuple[str, ...] = ()
    recent_first_seen_bonus: int = 2

    def __post_init__(self) -> None:
        for field_name in (
            "hot_max_position",
            "reply_growth_min",
            "like_growth_min",
            "recent_first_seen_bonus",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} cannot be negative")
        object.__setattr__(
            self,
            "controversy_keywords",
            _normalize_keywords(self.controversy_keywords),
        )

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> WatchlistPolicy:
        values = config or {}
        keywords = values.get("controversy_keywords", ())
        if isinstance(keywords, str) or not isinstance(keywords, Sequence):
            raise ValueError("controversy_keywords must be a list of strings")
        return cls(
            hot_max_position=int(values.get("hot_max_position", 3)),
            reply_growth_min=int(values.get("reply_growth_min", 5)),
            like_growth_min=int(values.get("like_growth_min", 20)),
            controversy_keywords=tuple(str(value) for value in keywords),
            recent_first_seen_bonus=int(values.get("recent_first_seen_bonus", 2)),
        )


@dataclass(frozen=True, slots=True)
class WatchlistPriority:
    reason: str
    priority: int
    score: float
    extra: dict[str, Any]


def calculate_watchlist_priority(
    *,
    policy: WatchlistPolicy,
    content: str | None,
    sort_mode: str,
    position: int | None,
    previous_reply_count: int | None,
    current_reply_count: int | None,
    previous_like_count: int | None,
    current_like_count: int | None,
    is_first_seen: bool,
) -> WatchlistPriority | None:
    signals: list[tuple[str, int, float]] = []
    extra: dict[str, Any] = {}

    if (
        sort_mode == "hot"
        and position is not None
        and 0 <= position <= policy.hot_max_position
    ):
        hot_priority = 100 - position
        signals.append(("hot_top", hot_priority, float(hot_priority)))
        extra["hot_position"] = position

    if previous_reply_count is not None and current_reply_count is not None:
        reply_delta = current_reply_count - previous_reply_count
        if reply_delta >= policy.reply_growth_min:
            signals.append(
                ("reply_growth", 80 + min(reply_delta, 19), float(reply_delta))
            )
            extra["reply_delta"] = reply_delta

    if previous_like_count is not None and current_like_count is not None:
        like_delta = current_like_count - previous_like_count
        if like_delta >= policy.like_growth_min:
            signals.append(
                ("like_growth", 70 + min(like_delta // 5, 19), like_delta / 5)
            )
            extra["like_delta"] = like_delta

    normalized_content = (content or "").casefold()
    matched_keywords = [
        keyword
        for keyword in policy.controversy_keywords
        if keyword in normalized_content
    ]
    if matched_keywords:
        signals.append(
            (
                "controversy_keyword",
                75 + min(len(matched_keywords) - 1, 4),
                float(len(matched_keywords) * 10),
            )
        )
        extra["controversy_keywords"] = matched_keywords

    if not signals:
        return None

    reason, base_priority, _primary_score = max(signals, key=lambda signal: signal[1])
    recent_bonus = policy.recent_first_seen_bonus if is_first_seen else 0
    if is_first_seen:
        extra["recent_first_seen"] = True
    return WatchlistPriority(
        reason=reason,
        priority=min(base_priority + recent_bonus, 100),
        score=sum(signal[2] for signal in signals) + recent_bonus,
        extra=extra,
    )


def _normalize_keywords(values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        keyword = " ".join(str(value).strip().split()).casefold()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        normalized.append(keyword)
    return tuple(normalized)
