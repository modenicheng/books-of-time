from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from books_of_time.db.base import Base, TimestampMixin
from books_of_time.db.types import UTCDateTime, bigint_pk_type, json_dict_type
from books_of_time.domain.enums import (
    BilibiliRequestType,
    ScheduledJobKind,
    TaskKind,
    TaskStatus,
)


class RawPayload(Base):
    __tablename__ = "raw_payloads"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    request_type: Mapped[BilibiliRequestType] = mapped_column(
        Enum(BilibiliRequestType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    method: Mapped[str] = mapped_column(String(12), nullable=False)
    url_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    params_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    status_code: Mapped[int | None] = mapped_column(Integer)
    payload_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    compressed_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uncompressed_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )


Index("idx_raw_payloads_time", RawPayload.captured_at.desc())
Index(
    "idx_raw_payloads_request_type_time",
    RawPayload.request_type,
    RawPayload.captured_at.desc(),
)


class RawPageObservation(Base):
    __tablename__ = "raw_page_observations"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    raw_payload_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    request_type: Mapped[BilibiliRequestType] = mapped_column(
        Enum(BilibiliRequestType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    cursor: Mapped[str | None] = mapped_column(Text)
    sort_mode: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index(
    "idx_raw_page_observations_target_time",
    RawPageObservation.target_type,
    RawPageObservation.target_id,
    RawPageObservation.captured_at.desc(),
)
Index("idx_raw_page_observations_raw_payload", RawPageObservation.raw_payload_id)


class CommentEntity(TimestampMixin, Base):
    __tablename__ = "comment_entities"

    rpid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    oid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bvid: Mapped[str] = mapped_column(Text, nullable=False)
    root_rpid: Mapped[int | None] = mapped_column(BigInteger)
    parent_rpid: Mapped[int | None] = mapped_column(BigInteger)
    author_mid: Mapped[int | None] = mapped_column(BigInteger)
    author_name: Mapped[str | None] = mapped_column(Text)
    first_content: Mapped[str | None] = mapped_column(Text)
    first_content_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    first_raw_payload_id: Mapped[int | None] = mapped_column(BigInteger)


Index("idx_comment_entities_bvid_rpid", CommentEntity.bvid, CommentEntity.rpid)
Index("idx_comment_entities_author_mid", CommentEntity.author_mid)


class CommentObservation(Base):
    __tablename__ = "comment_observations"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    rpid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bvid: Mapped[str] = mapped_column(Text, nullable=False)
    oid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    raw_payload_id: Mapped[int | None] = mapped_column(BigInteger)
    raw_page_observation_id: Mapped[int | None] = mapped_column(BigInteger)
    sort_mode: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    media_ordered_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    media_set_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    like_count: Mapped[int | None] = mapped_column(BigInteger)
    reply_count: Mapped[int | None] = mapped_column(BigInteger)
    author_mid: Mapped[int | None] = mapped_column(BigInteger)
    author_name: Mapped[str | None] = mapped_column(Text)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, default="visible")
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index(
    "idx_comment_observations_bvid_time",
    CommentObservation.bvid,
    CommentObservation.captured_at.desc(),
)
Index(
    "idx_comment_observations_rpid_time",
    CommentObservation.rpid,
    CommentObservation.captured_at.desc(),
)
Index(
    "idx_comment_observations_raw_page",
    CommentObservation.raw_page_observation_id,
)


class CommentStateEvent(Base):
    __tablename__ = "comment_state_events"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    rpid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bvid: Mapped[str] = mapped_column(Text, nullable=False)
    previous_comment_observation_id: Mapped[int | None] = mapped_column(BigInteger)
    current_comment_observation_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
    new_value: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


Index("idx_comment_state_events_rpid", CommentStateEvent.rpid)
Index(
    "idx_comment_state_events_current",
    CommentStateEvent.current_comment_observation_id,
)


class CommentVisibilityEvent(Base):
    __tablename__ = "comment_visibility_events"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    rpid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bvid: Mapped[str] = mapped_column(Text, nullable=False)
    previous_comment_observation_id: Mapped[int | None] = mapped_column(BigInteger)
    current_comment_observation_id: Mapped[int | None] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    old_visibility: Mapped[str | None] = mapped_column(Text)
    new_visibility: Mapped[str | None] = mapped_column(Text)
    missing_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


Index("idx_comment_visibility_events_rpid", CommentVisibilityEvent.rpid)
Index(
    "idx_comment_visibility_events_current",
    CommentVisibilityEvent.current_comment_observation_id,
)
Index(
    "idx_comment_visibility_events_type",
    CommentVisibilityEvent.event_type,
    CommentVisibilityEvent.created_at.desc(),
)


class ImportantCommentWatchlist(TimestampMixin, Base):
    __tablename__ = "important_comment_watchlist"
    __table_args__ = (UniqueConstraint("bvid", "rpid"),)

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    bvid: Mapped[str] = mapped_column(Text, nullable=False)
    rpid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    root_rpid: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reply_count: Mapped[int | None] = mapped_column(BigInteger)
    like_count: Mapped[int | None] = mapped_column(BigInteger)
    hot_position: Mapped[int | None] = mapped_column(Integer)
    last_comment_observation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index("idx_important_watchlist_active", ImportantCommentWatchlist.active)
Index(
    "idx_important_watchlist_priority",
    ImportantCommentWatchlist.active,
    ImportantCommentWatchlist.priority.desc(),
    ImportantCommentWatchlist.updated_at.desc(),
)
Index("idx_important_watchlist_rpid", ImportantCommentWatchlist.rpid)


class MediaAsset(TimestampMixin, Base):
    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    blob_sha256: Mapped[bytes] = mapped_column(
        LargeBinary(32), nullable=False, unique=True
    )
    pixel_sha256: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    mime_type: Mapped[str | None] = mapped_column(Text)
    file_ext: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    first_raw_page_id: Mapped[int | None] = mapped_column(BigInteger)
    download_raw_payload_id: Mapped[int | None] = mapped_column(BigInteger)
    phash: Mapped[int | None] = mapped_column(BigInteger)
    dhash: Mapped[int | None] = mapped_column(BigInteger)
    ahash: Mapped[int | None] = mapped_column(BigInteger)


Index("idx_media_assets_pixel_sha256", MediaAsset.pixel_sha256)
Index("idx_media_assets_phash", MediaAsset.phash)


class MediaSource(TimestampMixin, Base):
    __tablename__ = "media_sources"
    __table_args__ = (UniqueConstraint("platform", "source_url_hash"),)

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False, default="bilibili")
    source_url_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    normalized_url_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    normalized_url: Mapped[str | None] = mapped_column(Text)
    media_asset_id: Mapped[int | None] = mapped_column(BigInteger)
    fetch_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    fetch_error_type: Mapped[str | None] = mapped_column(Text)
    fetch_error_message: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    first_raw_page_id: Mapped[int | None] = mapped_column(BigInteger)
    last_raw_page_id: Mapped[int | None] = mapped_column(BigInteger)


Index("idx_media_sources_asset", MediaSource.media_asset_id)
Index("idx_media_sources_fetch_status", MediaSource.fetch_status)
Index("idx_media_sources_normalized_url", MediaSource.normalized_url_hash)


class CommentObservationMedia(Base):
    __tablename__ = "comment_observation_media"
    __table_args__ = (UniqueConstraint("comment_observation_id", "position"),)

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    comment_observation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bvid: Mapped[str] = mapped_column(Text, nullable=False)
    rpid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_asset_id: Mapped[int | None] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str | None] = mapped_column(Text)
    raw_page_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


Index("idx_comment_obs_media_rpid", CommentObservationMedia.rpid)
Index("idx_comment_obs_media_asset", CommentObservationMedia.media_asset_id)
Index("idx_comment_obs_media_source", CommentObservationMedia.media_source_id)


class MediaSimilarityEdge(Base):
    __tablename__ = "media_similarity_edges"
    __table_args__ = (
        UniqueConstraint(
            "media_asset_id_a",
            "media_asset_id_b",
            "similarity_type",
            "algorithm",
            "algorithm_version",
        ),
    )

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    media_asset_id_a: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_asset_id_b: Mapped[int] = mapped_column(BigInteger, nullable=False)
    similarity_type: Mapped[str] = mapped_column(Text, nullable=False)
    distance: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


Index("idx_media_similarity_a", MediaSimilarityEdge.media_asset_id_a)
Index("idx_media_similarity_b", MediaSimilarityEdge.media_asset_id_b)


class MediaCluster(Base):
    __tablename__ = "media_clusters"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    cluster_type: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(Text, nullable=False)
    representative_asset_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class MediaClusterMember(Base):
    __tablename__ = "media_cluster_members"

    cluster_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    media_asset_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    distance_to_representative: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)


class VideoMetricSnapshot(Base):
    __tablename__ = "video_metric_snapshots"

    bvid: Mapped[str] = mapped_column(Text, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    view_count: Mapped[int | None] = mapped_column(BigInteger)
    like_count: Mapped[int | None] = mapped_column(BigInteger)
    coin_count: Mapped[int | None] = mapped_column(BigInteger)
    favorite_count: Mapped[int | None] = mapped_column(BigInteger)
    share_count: Mapped[int | None] = mapped_column(BigInteger)
    reply_count: Mapped[int | None] = mapped_column(BigInteger)
    danmaku_count: Mapped[int | None] = mapped_column(BigInteger)
    raw_payload_id: Mapped[int | None] = mapped_column(BigInteger)


class VideoInfoSnapshot(Base):
    __tablename__ = "video_info_snapshots"

    bvid: Mapped[str] = mapped_column(Text, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    owner_mid: Mapped[int | None] = mapped_column(BigInteger)
    owner_name: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
    raw_payload_id: Mapped[int | None] = mapped_column(BigInteger)


Index(
    "idx_video_info_snapshots_bvid_time",
    VideoInfoSnapshot.bvid,
    VideoInfoSnapshot.captured_at.desc(),
)


class VideoAvailabilitySnapshot(Base):
    __tablename__ = "video_availability_snapshots"

    bvid: Mapped[str] = mapped_column(Text, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    bili_code: Mapped[int | None] = mapped_column(BigInteger)
    bili_message: Mapped[str | None] = mapped_column(Text)
    http_status_code: Mapped[int | None] = mapped_column(Integer)
    raw_payload_id: Mapped[int | None] = mapped_column(BigInteger)


Index(
    "idx_video_availability_snapshots_bvid_time",
    VideoAvailabilitySnapshot.bvid,
    VideoAvailabilitySnapshot.captured_at.desc(),
)
Index(
    "idx_video_availability_snapshots_status_time",
    VideoAvailabilitySnapshot.status,
    VideoAvailabilitySnapshot.captured_at.desc(),
)


class CollectionTask(TimestampMixin, Base):
    __tablename__ = "collection_tasks"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    kind: Mapped[TaskKind] = mapped_column(
        Enum(TaskKind, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    budget_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=TaskStatus.PENDING,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
    not_before: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)


Index(
    "idx_collection_tasks_pick",
    CollectionTask.status,
    CollectionTask.not_before,
    CollectionTask.priority.desc(),
    CollectionTask.created_at,
)
Index(
    "idx_collection_tasks_target",
    CollectionTask.target_type,
    CollectionTask.target_id,
    CollectionTask.status,
)
Index(
    "uq_collection_tasks_active_idempotency_key",
    CollectionTask.idempotency_key,
    unique=True,
    sqlite_where=CollectionTask.status.in_(
        [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.BACKOFF]
    ),
    postgresql_where=CollectionTask.status.in_(
        [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.BACKOFF]
    ),
)


class CollectionRun(TimestampMixin, Base):
    __tablename__ = "collection_runs"
    __table_args__ = (UniqueConstraint("run_id"),)

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    tasks_started: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index("idx_collection_runs_run_id", CollectionRun.run_id)
Index("idx_collection_runs_started_at", CollectionRun.started_at.desc())


class CollectionCoverageStat(TimestampMixin, Base):
    __tablename__ = "collection_coverage_stats"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    collection_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_kind: Mapped[TaskKind] = mapped_column(
        Enum(TaskKind, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    pages_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_payloads_saved: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    parse_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frontier_reached: Mapped[bool | None] = mapped_column(Boolean)
    frontier_missing: Mapped[bool | None] = mapped_column(Boolean)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    corrupted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index(
    "idx_collection_coverage_target_time",
    CollectionCoverageStat.target_type,
    CollectionCoverageStat.target_id,
    CollectionCoverageStat.finished_at.desc(),
)
Index("idx_collection_coverage_task", CollectionCoverageStat.collection_task_id)
Index("idx_collection_coverage_run", CollectionCoverageStat.run_id)


class RequestBackoffState(TimestampMixin, Base):
    __tablename__ = "request_backoff_states"
    __table_args__ = (UniqueConstraint("platform", "request_type", "scope"),)

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    request_type: Mapped[BilibiliRequestType] = mapped_column(
        Enum(BilibiliRequestType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    error_kind: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_failed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_failed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    backoff_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_message: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index(
    "idx_request_backoff_key",
    RequestBackoffState.platform,
    RequestBackoffState.request_type,
    RequestBackoffState.scope,
)
Index("idx_request_backoff_until", RequestBackoffState.backoff_until)
Index(
    "idx_request_backoff_error_time",
    RequestBackoffState.error_kind,
    RequestBackoffState.last_failed_at.desc(),
)


class ServiceInstance(TimestampMixin, Base):
    __tablename__ = "service_instances"

    instance_id: Mapped[str] = mapped_column(Text, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    roles: Mapped[list[str]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=list,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error_type: Mapped[str | None] = mapped_column(String(120))
    last_error_message: Mapped[str | None] = mapped_column(String(2000))


Index(
    "idx_service_instances_status_heartbeat",
    ServiceInstance.status,
    ServiceInstance.heartbeat_at.desc(),
)
Index("idx_service_instances_started_at", ServiceInstance.started_at.desc())


class ScheduledJob(TimestampMixin, Base):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (UniqueConstraint("job_key"),)

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    job_key: Mapped[str] = mapped_column(Text, nullable=False)
    job_kind: Mapped[ScheduledJobKind] = mapped_column(
        Enum(ScheduledJobKind, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    schedule_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_succeeded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    last_error_type: Mapped[str | None] = mapped_column(String(120))
    last_error_message: Mapped[str | None] = mapped_column(String(2000))


Index(
    "idx_scheduled_jobs_due",
    ScheduledJob.enabled,
    ScheduledJob.next_run_at,
    ScheduledJob.priority.desc(),
)
Index("idx_scheduled_jobs_lease", ScheduledJob.lease_until)


class Event(TimestampMixin, Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    game: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    start_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    end_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Shanghai"
    )


Index("idx_events_status_time", Event.status, Event.start_at)
Index("idx_events_game", Event.game)


class EventTarget(TimestampMixin, Base):
    __tablename__ = "event_targets"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "target_type",
            "normalized_value",
            name="uq_event_targets_stable_key",
        ),
    )

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type, nullable=False, default=dict
    )


Index("idx_event_targets_event_active", EventTarget.event_id, EventTarget.active)
Index(
    "idx_event_targets_type_value",
    EventTarget.target_type,
    EventTarget.normalized_value,
)


class EventVideo(TimestampMixin, Base):
    __tablename__ = "event_videos"

    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("events.id", ondelete="CASCADE"),
        primary_key=True,
    )
    bvid: Mapped[str] = mapped_column(Text, primary_key=True)
    source_target_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("event_targets.id", ondelete="SET NULL")
    )
    association_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


Index("idx_event_videos_bvid", EventVideo.bvid)
Index("idx_event_videos_event_active", EventVideo.event_id, EventVideo.active)


class EventKeyword(TimestampMixin, Base):
    __tablename__ = "event_keywords"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "normalized_keyword",
            "version",
            name="uq_event_keywords_version",
        ),
    )

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    keyword: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_keyword: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="topic")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_target_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("event_targets.id", ondelete="SET NULL")
    )


Index("idx_event_keywords_event_active", EventKeyword.event_id, EventKeyword.active)
Index("idx_event_keywords_normalized", EventKeyword.normalized_keyword)


class KnownVideo(TimestampMixin, Base):
    __tablename__ = "known_videos"

    bvid: Mapped[str] = mapped_column(Text, primary_key=True)
    source_mid: Mapped[str | None] = mapped_column(Text)
    pubdate: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class FrontierState(TimestampMixin, Base):
    __tablename__ = "frontier_states"
    __table_args__ = (UniqueConstraint("target_type", "target_id", "frontier_type"),)

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    frontier_type: Mapped[str] = mapped_column(Text, nullable=False)
    frontier_rpid: Mapped[int | None] = mapped_column(BigInteger)
    frontier_time: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cursor: Mapped[str | None] = mapped_column(Text)
    last_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_scan_status: Mapped[str | None] = mapped_column(Text)
    last_scan_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_scan_truncated: Mapped[bool] = mapped_column(nullable=False, default=False)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
