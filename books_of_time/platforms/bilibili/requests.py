from __future__ import annotations

from typing import Any

from books_of_time.domain.enums import BilibiliRequestType


def classify_bilibili_request(
    url: str,
    params: dict[str, Any] | None = None,
) -> BilibiliRequestType:
    request_params = params or {}

    if "archive/stat" in url or "web-interface/view" in url:
        return BilibiliRequestType.VIDEO_STATS
    if "space" in url and "arc/search" in url:
        return BilibiliRequestType.USER_VIDEO_LIST
    if "reply" in url:
        mode = str(request_params.get("mode") or request_params.get("sort") or "")
        if mode in {"hot", "3"}:
            return BilibiliRequestType.COMMENT_HOT
        if mode in {"time", "2"}:
            return BilibiliRequestType.COMMENT_LATEST
        return BilibiliRequestType.COMMENT_REPLY
    if "search" in url:
        return BilibiliRequestType.SEARCH_VIDEO
    return BilibiliRequestType.DEFAULT
