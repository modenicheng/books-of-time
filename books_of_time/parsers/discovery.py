from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from books_of_time.task_orchestrator.discovery import DiscoveredVideo

DISCOVERY_PARSER_VERSION = "bilibili-user-video-list-v1"


def parse_user_video_list(
    payload: dict[str, Any],
    *,
    source_mid: str,
    source_pool_type: str | None = None,
    source_pool_id: str | None = None,
) -> list[DiscoveredVideo]:
    archives = payload.get("data", {}).get("list", {}).get("vlist", [])
    videos: list[DiscoveredVideo] = []
    for item in archives:
        bvid = item.get("bvid")
        pubdate = item.get("created") or item.get("pubdate")
        if not bvid or pubdate is None:
            continue
        videos.append(
            DiscoveredVideo(
                bvid=str(bvid),
                pubdate=datetime.fromtimestamp(int(pubdate), tz=UTC),
                source_mid=source_mid,
                source_pool_type=source_pool_type,
                source_pool_id=source_pool_id,
            )
        )
    return videos
