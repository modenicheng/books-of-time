from __future__ import annotations

from enum import StrEnum


class BilibiliRequestType(StrEnum):
    VIDEO_INFO = "bilibili:video_info"
    VIDEO_STATS = "bilibili:video_stats"
    COMMENT_HOT = "bilibili:comment_hot"
    COMMENT_LATEST = "bilibili:comment_latest"
    COMMENT_REPLY = "bilibili:comment_reply"
    MEDIA_IMAGE = "bilibili:media_image"
    USER_VIDEO_LIST = "bilibili:user_video_list"
    SEARCH_VIDEO = "bilibili:search_video"
    DEFAULT = "bilibili:default"


class TaskKind(StrEnum):
    FETCH_VIDEO_INFO = "fetch_video_info"
    FETCH_VIDEO_STATS = "fetch_video_stats"
    FETCH_HOT_COMMENTS = "fetch_hot_comments"
    FETCH_LATEST_COMMENTS = "fetch_latest_comments"
    FETCH_COMMENT_REPLIES = "fetch_comment_replies"
    FETCH_MEDIA_ASSET = "fetch_media_asset"
    DISCOVER_USER_VIDEOS = "discover_user_videos"
    ANALYZE_SIMILAR_MEDIA = "analyze_similar_media"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BACKOFF = "backoff"


class CommentScanMode(StrEnum):
    HOT_CORE = "hot_core"
    HOT_DEEP = "hot_deep"
    BASELINE_TAIL = "baseline_tail"
    BASELINE_HEAD_SWEEP = "baseline_head_sweep"
    INCREMENTAL = "incremental"
    FULL_RECONCILIATION = "full_reconciliation"
    SEGMENTED_RECONCILIATION = "segmented_reconciliation"
    REPLY_REFRESH = "reply_refresh"
    VISIBILITY_PROBE = "visibility_probe"


class CommentScanStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CORRUPTED = "corrupted"


class ScheduledJobKind(StrEnum):
    UID_DISCOVERY = "uid_discovery"
    VIDEO_SNAPSHOT_SWEEP = "video_snapshot_sweep"
    DAILY_TERMINAL_SNAPSHOT = "daily_terminal_snapshot"
    ACCOUNT_COOKIE_REFRESH = "account_cookie_refresh"
    OPERATIONAL_ALERT_EVALUATION = "operational_alert_evaluation"
    SNAPSHOT_COHORT_PLANNING = "snapshot_cohort_planning"
