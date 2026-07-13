from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from books_of_time.task_orchestrator.discovery import (
    DiscoveredVideo,
    normalize_source_associations,
)

DISCOVERY_PARSER_VERSION = "bilibili-user-video-list-v2"


def parse_user_video_list(
    payload: dict[str, Any],
    *,
    source_mid: str,
    source_pool_type: str | None = None,
    source_pool_id: str | None = None,
    source_associations: list[dict[str, Any]] | None = None,
) -> list[DiscoveredVideo]:
    normalized_associations = normalize_source_associations(
        source_mid=source_mid,
        source_pool_type=source_pool_type,
        source_pool_id=source_pool_id,
        source_associations=source_associations,
    )
    primary_source = normalized_associations[0]
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
                source_mid=str(primary_source["source_mid"]),
                source_pool_type=str(primary_source["pool_type"]),
                source_pool_id=str(primary_source["pool_id"]),
                source_associations=tuple(
                    dict(association) for association in normalized_associations
                ),
            )
        )
    return videos
