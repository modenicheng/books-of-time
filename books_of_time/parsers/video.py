from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

VIDEO_PARSER_VERSION = "video_parser_v0.1.0"


@dataclass(frozen=True)
class ParsedVideoStats:
    bvid: str
    captured_at: datetime
    view_count: int | None
    like_count: int | None
    coin_count: int | None
    favorite_count: int | None
    share_count: int | None
    reply_count: int | None
    danmaku_count: int | None
    raw_payload_id: int | None


def parse_video_stats(
    payload: dict[str, Any],
    *,
    captured_at: datetime,
    raw_payload_id: int | None,
) -> ParsedVideoStats:
    data = payload.get("data") or {}
    stats = data.get("stat") or data
    return ParsedVideoStats(
        bvid=str(data["bvid"]),
        captured_at=captured_at,
        view_count=stats.get("view"),
        like_count=stats.get("like"),
        coin_count=stats.get("coin"),
        favorite_count=stats.get("favorite"),
        share_count=stats.get("share"),
        reply_count=stats.get("reply"),
        danmaku_count=stats.get("danmaku"),
        raw_payload_id=raw_payload_id,
    )
