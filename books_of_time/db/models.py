from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from books_of_time.db.base import Base, TimestampMixin
from books_of_time.db.types import UTCDateTime, bigint_pk_type, json_dict_type
from books_of_time.domain.enums import (
    BilibiliRequestType,
    CommentScanMode,
    CommentScanStatus,
    ScheduledJobKind,
    TaskKind,
    TaskStatus,
)


def _postgresql_brin_index(name: str, column):
    return Index(
        name,
        column,
        info={"postgresql_only": True},
        postgresql_using="brin",
        postgresql_with={"pages_per_range": 128, "autosummarize": True},
    ).ddl_if(dialect="postgresql")


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
_postgresql_brin_index("idx_raw_payloads_captured_brin", RawPayload.captured_at)


class RawPageObservation(Base):
    __tablename__ = "raw_page_observations"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    raw_payload_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scan_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("comment_scan_runs.id", ondelete="SET NULL"),
    )
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
Index("idx_raw_page_observations_scan_run", RawPageObservation.scan_run_id)
_postgresql_brin_index(
    "idx_raw_page_observations_captured_brin",
    RawPageObservation.captured_at,
)


class CommentEntity(TimestampMixin, Base):
    __tablename__ = "comment_entities"

    rpid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    oid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bvid: Mapped[str] = mapped_column(Text, nullable=False)
    root_rpid: Mapped[int | None] = mapped_column(BigInteger)
    parent_rpid: Mapped[int | None] = mapped_column(BigInteger)
    author_mid: Mapped[int | None] = mapped_column(BigInteger)
    author_name: Mapped[str | None] = mapped_column(Text)
    platform_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    author_level: Mapped[int | None] = mapped_column(Integer)
    author_official_type: Mapped[int | None] = mapped_column(Integer)
    author_official_description: Mapped[str | None] = mapped_column(Text)
    author_vip_status: Mapped[int | None] = mapped_column(Integer)
    author_vip_type: Mapped[int | None] = mapped_column(Integer)
    author_is_senior_member: Mapped[bool | None] = mapped_column(Boolean)
    author_public_metadata_extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
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
    scan_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("comment_scan_runs.id", ondelete="SET NULL"),
    )
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
    platform_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    author_level: Mapped[int | None] = mapped_column(Integer)
    author_official_type: Mapped[int | None] = mapped_column(Integer)
    author_official_description: Mapped[str | None] = mapped_column(Text)
    author_vip_status: Mapped[int | None] = mapped_column(Integer)
    author_vip_type: Mapped[int | None] = mapped_column(Integer)
    author_is_senior_member: Mapped[bool | None] = mapped_column(Boolean)
    author_public_metadata_extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
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
Index("idx_comment_observations_scan_run", CommentObservation.scan_run_id)
_postgresql_brin_index(
    "idx_comment_observations_captured_brin",
    CommentObservation.captured_at,
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
_postgresql_brin_index(
    "idx_comment_state_events_created_brin",
    CommentStateEvent.created_at,
)
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
_postgresql_brin_index(
    "idx_comment_visibility_events_created_brin",
    CommentVisibilityEvent.created_at,
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


_postgresql_brin_index(
    "idx_video_metric_snapshots_captured_brin",
    VideoMetricSnapshot.captured_at,
)


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
_postgresql_brin_index(
    "idx_video_info_snapshots_captured_brin",
    VideoInfoSnapshot.captured_at,
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
_postgresql_brin_index(
    "idx_video_availability_snapshots_captured_brin",
    VideoAvailabilitySnapshot.captured_at,
)


class CollectionTask(TimestampMixin, Base):
    __tablename__ = "collection_tasks"
    __table_args__ = (
        CheckConstraint(
            "scan_slice_no IS NULL OR scan_slice_no >= 0",
            name="ck_collection_tasks_scan_slice_no",
        ),
    )

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
    snapshot_cohort_id: Mapped[int | None] = mapped_column(BigInteger)
    snapshot_cohort_component_id: Mapped[int | None] = mapped_column(BigInteger)
    comment_scan_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("comment_scan_runs.id", ondelete="SET NULL"),
    )
    scan_slice_no: Mapped[int | None] = mapped_column(Integer)
    scan_slice_key: Mapped[str | None] = mapped_column(Text)


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
Index(
    "uq_collection_tasks_scan_slice_key",
    CollectionTask.scan_slice_key,
    unique=True,
)
Index(
    "idx_collection_tasks_scan_run_slice",
    CollectionTask.comment_scan_run_id,
    CollectionTask.scan_slice_no,
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
    snapshot_cohort_id: Mapped[int | None] = mapped_column(BigInteger)
    snapshot_cohort_component_id: Mapped[int | None] = mapped_column(BigInteger)
    comment_scan_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("comment_scan_runs.id", ondelete="SET NULL"),
    )
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
Index("idx_collection_coverage_scan_run", CollectionCoverageStat.comment_scan_run_id)


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


class HttpRequestAttempt(Base):
    __tablename__ = "http_request_attempts"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    collection_task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("collection_tasks.id", ondelete="SET NULL"),
    )
    snapshot_cohort_id: Mapped[int | None] = mapped_column(BigInteger)
    snapshot_cohort_component_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    request_type: Mapped[BilibiliRequestType] = mapped_column(
        Enum(BilibiliRequestType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    attempt_started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    request_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    request_finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    response_received_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(12), nullable=False)
    url_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    params_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    raw_payload_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("raw_payloads.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


Index(
    "idx_http_request_attempts_status_time",
    HttpRequestAttempt.status,
    HttpRequestAttempt.attempt_started_at.desc(),
)
Index("idx_http_request_attempts_task", HttpRequestAttempt.collection_task_id)
Index("idx_http_request_attempts_raw", HttpRequestAttempt.raw_payload_id)


class RequestBudgetState(Base):
    __tablename__ = "request_budget_states"

    budget_key: Mapped[str] = mapped_column(Text, primary_key=True)
    tokens: Mapped[float] = mapped_column(Float, nullable=False)
    refill_rate: Mapped[float] = mapped_column(Float, nullable=False)
    burst: Mapped[int] = mapped_column(Integer, nullable=False)
    last_refill_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


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


class OperationalAlertState(TimestampMixin, Base):
    __tablename__ = "operational_alert_states"

    alert_key: Mapped[str] = mapped_column(Text, primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
    first_triggered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_triggered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    occurrence_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )


Index(
    "idx_operational_alert_states_status_severity",
    OperationalAlertState.status,
    OperationalAlertState.severity,
)
Index(
    "idx_operational_alert_states_type",
    OperationalAlertState.alert_type,
)


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


class CommentAnalysisFlag(Base):
    __tablename__ = "comment_analysis_flags"

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    stable_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    flag_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_rpid: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("comment_entities.rpid", ondelete="CASCADE"),
        nullable=False,
    )
    related_rpid: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("comment_entities.rpid", ondelete="CASCADE"),
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(160), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type, nullable=False, default=dict
    )
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


Index(
    "idx_comment_analysis_flags_event_type",
    CommentAnalysisFlag.event_id,
    CommentAnalysisFlag.flag_type,
    CommentAnalysisFlag.detected_at.desc(),
)
Index("idx_comment_analysis_flags_subject", CommentAnalysisFlag.subject_rpid)
Index("idx_comment_analysis_flags_related", CommentAnalysisFlag.related_rpid)


class KnownVideo(TimestampMixin, Base):
    __tablename__ = "known_videos"

    bvid: Mapped[str] = mapped_column(Text, primary_key=True)
    source_mid: Mapped[str | None] = mapped_column(Text)
    pubdate: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class KnownVideoSource(TimestampMixin, Base):
    __tablename__ = "known_video_sources"
    __table_args__ = (
        UniqueConstraint(
            "bvid",
            "source_mid",
            "pool_type",
            "pool_id",
            name="uq_known_video_sources_identity",
        ),
    )

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    bvid: Mapped[str] = mapped_column(
        Text,
        ForeignKey("known_videos.bvid", ondelete="CASCADE"),
        nullable=False,
    )
    source_mid: Mapped[str] = mapped_column(Text, nullable=False)
    pool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    pool_id: Mapped[str] = mapped_column(Text, nullable=False)
    game_id: Mapped[str | None] = mapped_column(String(120))
    official: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    monitored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    first_raw_page_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("raw_page_observations.id", ondelete="SET NULL"),
    )
    last_raw_page_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("raw_page_observations.id", ondelete="SET NULL"),
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


Index(
    "idx_known_video_sources_bvid_active",
    KnownVideoSource.bvid,
    KnownVideoSource.active,
)
Index(
    "idx_known_video_sources_game_flags",
    KnownVideoSource.game_id,
    KnownVideoSource.official,
    KnownVideoSource.monitored,
)
Index("idx_known_video_sources_mid", KnownVideoSource.source_mid)


class CollectionPolicyVersion(Base):
    __tablename__ = "collection_policy_versions"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('global', 'game')",
            name="ck_collection_policy_versions_scope_type",
        ),
        CheckConstraint(
            "distinct_comment_count >= 0",
            name="ck_collection_policy_versions_distinct_comments",
        ),
        CheckConstraint(
            "complete_day_count >= 0",
            name="ck_collection_policy_versions_complete_days",
        ),
        CheckConstraint(
            "valid_exposure_minutes >= 0",
            name="ck_collection_policy_versions_exposure_minutes",
        ),
        CheckConstraint(
            "excluded_comment_count >= 0",
            name="ck_collection_policy_versions_excluded_comments",
        ),
        CheckConstraint(
            "training_window_end IS NULL OR training_window_start IS NULL "
            "OR training_window_end > training_window_start",
            name="ck_collection_policy_versions_training_window",
        ),
    )

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    version: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    policy_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
    training_window_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    training_window_end: Mapped[datetime | None] = mapped_column(UTCDateTime())
    distinct_comment_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    complete_day_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_exposure_minutes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    excluded_comment_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    exclusion_reasons: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
    algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


Index(
    "uq_collection_policy_versions_active_scope",
    CollectionPolicyVersion.policy_kind,
    CollectionPolicyVersion.scope_type,
    CollectionPolicyVersion.scope_id,
    unique=True,
    sqlite_where=text("active = 1"),
    postgresql_where=text("active"),
)


class VideoCollectionState(Base):
    __tablename__ = "video_collection_states"
    __table_args__ = (
        CheckConstraint(
            "desired_tier IN ('s', 'a', 'b', 'c')",
            name="ck_video_collection_states_desired_tier",
        ),
        CheckConstraint(
            "effective_tier IN ('s', 'a', 'b', 'c')",
            name="ck_video_collection_states_effective_tier",
        ),
        CheckConstraint(
            "candidate_downgrade_tier IS NULL "
            "OR candidate_downgrade_tier IN ('s', 'a', 'b', 'c')",
            name="ck_video_collection_states_candidate_tier",
        ),
        CheckConstraint(
            "pinned_tier IS NULL OR pinned_tier IN ('s', 'a', 'b', 'c')",
            name="ck_video_collection_states_pinned_tier",
        ),
        CheckConstraint(
            "life_stage IN ('active', 'dormant', 'archived')",
            name="ck_video_collection_states_life_stage",
        ),
        CheckConstraint(
            "consecutive_downgrade_count >= 0",
            name="ck_video_collection_states_downgrade_count",
        ),
        CheckConstraint(
            "last_checkpoint_hours IS NULL OR last_checkpoint_hours > 0",
            name="ck_video_collection_states_checkpoint_hours",
        ),
    )

    bvid: Mapped[str] = mapped_column(
        Text,
        ForeignKey("known_videos.bvid", ondelete="CASCADE"),
        primary_key=True,
    )
    desired_tier: Mapped[str] = mapped_column(String(1), nullable=False, default="c")
    effective_tier: Mapped[str] = mapped_column(String(1), nullable=False, default="c")
    candidate_downgrade_tier: Mapped[str | None] = mapped_column(String(1))
    consecutive_downgrade_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    pinned_tier: Mapped[str | None] = mapped_column(String(1))
    life_stage: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active"
    )
    schedule_anchor_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    next_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_planned_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_completed_cohort_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_checkpoint_hours: Mapped[int | None] = mapped_column(Integer)
    policy_version: Mapped[str] = mapped_column(
        Text,
        ForeignKey("collection_policy_versions.version"),
        nullable=False,
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )


Index("idx_video_collection_states_next_due", VideoCollectionState.next_due_at)
Index(
    "idx_video_collection_states_life_stage",
    VideoCollectionState.life_stage,
    VideoCollectionState.next_due_at,
)


class SnapshotCohort(Base):
    __tablename__ = "snapshot_cohorts"
    __table_args__ = (
        CheckConstraint(
            "desired_tier IN ('s', 'a', 'b', 'c')",
            name="ck_snapshot_cohorts_desired_tier",
        ),
        CheckConstraint(
            "effective_tier IN ('s', 'a', 'b', 'c')",
            name="ck_snapshot_cohorts_effective_tier",
        ),
        CheckConstraint(
            "status IN ('planned', 'shadow_planned', 'running', 'complete', "
            "'partial', 'missed', 'corrupted', 'blocked', 'not_applicable')",
            name="ck_snapshot_cohorts_status",
        ),
        CheckConstraint(
            "age_checkpoint_hours IS NULL OR age_checkpoint_hours > 0",
            name="ck_snapshot_cohorts_checkpoint_hours",
        ),
        CheckConstraint(
            "expected_component_count >= 0",
            name="ck_snapshot_cohorts_expected_components",
        ),
        CheckConstraint(
            "completed_component_count >= 0",
            name="ck_snapshot_cohorts_completed_components",
        ),
        CheckConstraint(
            "completed_component_count <= expected_component_count",
            name="ck_snapshot_cohorts_component_counts",
        ),
    )

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    cohort_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    bvid: Mapped[str] = mapped_column(
        Text,
        ForeignKey("known_videos.bvid", ondelete="CASCADE"),
        nullable=False,
    )
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    age_checkpoint_hours: Mapped[int | None] = mapped_column(Integer)
    desired_tier: Mapped[str] = mapped_column(String(1), nullable=False)
    effective_tier: Mapped[str] = mapped_column(String(1), nullable=False)
    policy_version: Mapped[str] = mapped_column(
        Text,
        ForeignKey("collection_policy_versions.version"),
        nullable=False,
    )
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="planned")
    status_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expected_component_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    completed_component_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )


Index(
    "idx_snapshot_cohorts_bvid_scheduled",
    SnapshotCohort.bvid,
    SnapshotCohort.scheduled_for,
)
Index(
    "idx_snapshot_cohorts_status_deadline",
    SnapshotCohort.status,
    SnapshotCohort.deadline,
)


class SnapshotCohortComponent(Base):
    __tablename__ = "snapshot_cohort_components"
    __table_args__ = (
        UniqueConstraint(
            "cohort_id",
            "component_kind",
            name="uq_snapshot_cohort_components_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'complete', 'partial', "
            "'joined_active_task', 'missed_due_to_capacity', "
            "'missed_due_to_service_gap', 'failed', 'corrupted', "
            "'not_applicable', 'blocked')",
            name="ck_snapshot_cohort_components_status",
        ),
        CheckConstraint(
            "planned_pages >= 0",
            name="ck_snapshot_cohort_components_planned_pages",
        ),
        CheckConstraint(
            "requested_pages >= 0",
            name="ck_snapshot_cohort_components_requested_pages",
        ),
        CheckConstraint(
            "succeeded_pages >= 0",
            name="ck_snapshot_cohort_components_succeeded_pages",
        ),
        CheckConstraint(
            "items_observed >= 0",
            name="ck_snapshot_cohort_components_items_observed",
        ),
        CheckConstraint(
            "raw_payloads_saved >= 0",
            name="ck_snapshot_cohort_components_raw_payloads",
        ),
        CheckConstraint(
            "succeeded_pages <= requested_pages",
            name="ck_snapshot_cohort_components_page_counts",
        ),
    )

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    cohort_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("snapshot_cohorts.id", ondelete="CASCADE"),
        nullable=False,
    )
    component_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    skew_seconds: Mapped[int | None] = mapped_column(Integer)
    planned_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_observed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    raw_payloads_saved: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    comment_scan_run_id: Mapped[int | None] = mapped_column(BigInteger)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
    )


Index(
    "idx_snapshot_cohort_components_status_deadline",
    SnapshotCohortComponent.status,
    SnapshotCohortComponent.deadline,
)
Index(
    "idx_snapshot_cohort_components_scan_run",
    SnapshotCohortComponent.comment_scan_run_id,
)


class CommentScanRun(Base):
    __tablename__ = "comment_scan_runs"
    __table_args__ = (
        CheckConstraint(
            "target_pages IS NULL OR target_pages >= 0",
            name="ck_comment_scan_runs_target_pages",
        ),
        CheckConstraint(
            "next_page_number IS NULL OR next_page_number > 0",
            name="ck_comment_scan_runs_next_page",
        ),
        CheckConstraint(
            "pages_requested >= 0",
            name="ck_comment_scan_runs_pages_requested",
        ),
        CheckConstraint(
            "pages_succeeded >= 0",
            name="ck_comment_scan_runs_pages_succeeded",
        ),
        CheckConstraint(
            "items_observed >= 0",
            name="ck_comment_scan_runs_items_observed",
        ),
        CheckConstraint(
            "raw_payloads_saved >= 0",
            name="ck_comment_scan_runs_raw_payloads",
        ),
        CheckConstraint(
            "slice_count >= 0",
            name="ck_comment_scan_runs_slice_count",
        ),
        CheckConstraint(
            "pages_succeeded <= pages_requested",
            name="ck_comment_scan_runs_page_counts",
        ),
    )

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    scan_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    bvid: Mapped[str] = mapped_column(
        Text,
        ForeignKey("known_videos.bvid", ondelete="CASCADE"),
        nullable=False,
    )
    oid: Mapped[int | None] = mapped_column(BigInteger)
    snapshot_cohort_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("snapshot_cohorts.id", ondelete="SET NULL"),
    )
    parent_scan_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("comment_scan_runs.id", ondelete="SET NULL"),
    )
    mode: Mapped[CommentScanMode] = mapped_column(
        Enum(CommentScanMode, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    status: Mapped[CommentScanStatus] = mapped_column(
        Enum(CommentScanStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=CommentScanStatus.PLANNED,
    )
    outcome: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    start_frontier_rpid: Mapped[int | None] = mapped_column(BigInteger)
    result_frontier_rpid: Mapped[int | None] = mapped_column(BigInteger)
    start_anchor_set: Mapped[list[dict[str, Any]]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    result_anchor_set: Mapped[list[dict[str, Any]]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    start_cursor: Mapped[str | None] = mapped_column(Text)
    result_cursor: Mapped[str | None] = mapped_column(Text)
    target_pages: Mapped[int | None] = mapped_column(Integer)
    next_page_number: Mapped[int | None] = mapped_column(Integer)
    pages_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_observed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    raw_payloads_saved: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    slice_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error_type: Mapped[str | None] = mapped_column(String(120))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(
        Text,
        ForeignKey("collection_policy_versions.version"),
        nullable=False,
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )


Index(
    "idx_comment_scan_runs_bvid_mode_status",
    CommentScanRun.bvid,
    CommentScanRun.mode,
    CommentScanRun.status,
)
Index("idx_comment_scan_runs_cohort", CommentScanRun.snapshot_cohort_id)
Index(
    "idx_comment_scan_runs_status_updated",
    CommentScanRun.status,
    CommentScanRun.updated_at,
)
_active_latest_scan_predicate = CommentScanRun.mode.in_(
    (
        CommentScanMode.BASELINE_TAIL,
        CommentScanMode.BASELINE_HEAD_SWEEP,
        CommentScanMode.INCREMENTAL,
        CommentScanMode.FULL_RECONCILIATION,
        CommentScanMode.SEGMENTED_RECONCILIATION,
    )
) & CommentScanRun.status.in_(
    (
        CommentScanStatus.PLANNED,
        CommentScanStatus.RUNNING,
        CommentScanStatus.PAUSED,
    )
)
Index(
    "uq_comment_scan_runs_active_latest_bvid",
    CommentScanRun.bvid,
    unique=True,
    sqlite_where=_active_latest_scan_predicate,
    postgresql_where=_active_latest_scan_predicate,
)


class CollectionScheduleGap(Base):
    __tablename__ = "collection_schedule_gaps"
    __table_args__ = (
        UniqueConstraint(
            "bvid",
            "gap_start",
            "gap_end",
            "reason",
            "policy_version",
            name="uq_collection_schedule_gaps_identity",
        ),
        CheckConstraint(
            "expected_cohort_count >= 0",
            name="ck_collection_schedule_gaps_expected_count",
        ),
        CheckConstraint(
            "gap_end > gap_start",
            name="ck_collection_schedule_gaps_time_order",
        ),
    )

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    bvid: Mapped[str] = mapped_column(
        Text,
        ForeignKey("known_videos.bvid", ondelete="CASCADE"),
        nullable=False,
    )
    gap_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    gap_end: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expected_cohort_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    service_instance_id: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(
        Text,
        ForeignKey("collection_policy_versions.version"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )


Index(
    "idx_collection_schedule_gaps_bvid_time",
    CollectionScheduleGap.bvid,
    CollectionScheduleGap.gap_start,
    CollectionScheduleGap.gap_end,
)


class FrontierState(TimestampMixin, Base):
    __tablename__ = "frontier_states"
    __table_args__ = (
        UniqueConstraint("target_type", "target_id", "frontier_type"),
        CheckConstraint("version >= 0", name="ck_frontier_states_version"),
    )

    id: Mapped[int] = mapped_column(
        bigint_pk_type, primary_key=True, autoincrement=True
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    frontier_type: Mapped[str] = mapped_column(Text, nullable=False)
    frontier_rpid: Mapped[int | None] = mapped_column(BigInteger)
    frontier_time: Mapped[datetime | None] = mapped_column(UTCDateTime())
    frontier_anchor_set: Mapped[list[dict[str, Any]]] = mapped_column(
        json_dict_type,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    active_scan_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("comment_scan_runs.id", ondelete="SET NULL"),
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
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


Index("idx_frontier_states_active_scan", FrontierState.active_scan_run_id)
