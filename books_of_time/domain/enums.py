from __future__ import annotations

from enum import StrEnum


class BilibiliRequestType(StrEnum):
    VIDEO_INFO = "bilibili:video_info"
    VIDEO_STATS = "bilibili:video_stats"
    COMMENT_HOT = "bilibili:comment_hot"
    COMMENT_LATEST = "bilibili:comment_latest"
    COMMENT_REPLY = "bilibili:comment_reply"
    USER_VIDEO_LIST = "bilibili:user_video_list"
    SEARCH_VIDEO = "bilibili:search_video"
    DEFAULT = "bilibili:default"


class TaskKind(StrEnum):
    FETCH_VIDEO_INFO = "fetch_video_info"
    FETCH_VIDEO_STATS = "fetch_video_stats"
    FETCH_HOT_COMMENTS = "fetch_hot_comments"
    FETCH_LATEST_COMMENTS = "fetch_latest_comments"
    FETCH_COMMENT_REPLIES = "fetch_comment_replies"
    DISCOVER_USER_VIDEOS = "discover_user_videos"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BACKOFF = "backoff"
