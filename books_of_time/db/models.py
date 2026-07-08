from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from books_of_time.db.base import Base, TimestampMixin
from books_of_time.db.types import UTCDateTime, bigint_pk_type, json_dict_type
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus


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
