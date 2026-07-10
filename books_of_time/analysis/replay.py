from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import (
    CommentAnalysisFlag,
    CommentEntity,
    CommentObservation,
    CommentObservationMedia,
    CommentVisibilityEvent,
    EventVideo,
    RawPageObservation,
    VideoMetricSnapshot,
)
from books_of_time.db.repositories import EventRepository
from books_of_time.domain.enums import BilibiliRequestType

_METRIC_FIELDS: Final[tuple[str, ...]] = (
    "view_count",
    "like_count",
    "coin_count",
    "favorite_count",
    "share_count",
    "reply_count",
    "danmaku_count",
)


@dataclass(frozen=True, slots=True)
class VideoMetricReplayPoint:
    bvid: str
    captured_at: datetime
    previous_at: datetime | None
    elapsed_seconds: int | None
    metrics: dict[str, int | None]
    deltas: dict[str, int]
    raw_payload_id: int | None
    previous_raw_payload_id: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "video-metric-replay-v1",
            "bvid": self.bvid,
            "captured_at": self.captured_at.isoformat(),
            "previous_at": (
                self.previous_at.isoformat() if self.previous_at is not None else None
            ),
            "elapsed_seconds": self.elapsed_seconds,
            "metrics": self.metrics,
            "deltas": self.deltas,
            "raw_payload_id": self.raw_payload_id,
            "previous_raw_payload_id": self.previous_raw_payload_id,
        }


class VideoMetricReplayAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        *,
        bvid: str,
        since: datetime,
        until: datetime,
        max_points: int = 100_000,
    ) -> list[VideoMetricReplayPoint]:
        since_utc = _aware_utc(since, "since")
        until_utc = _aware_utc(until, "until")
        if until_utc <= since_utc:
            raise ValueError("until must be after since")
        if not 1 <= max_points <= 1_000_000:
            raise ValueError("max_points must be between 1 and 1000000")

        previous = await self.session.scalar(
            select(VideoMetricSnapshot)
            .where(
                VideoMetricSnapshot.bvid == bvid,
                VideoMetricSnapshot.captured_at < since_utc,
            )
            .order_by(VideoMetricSnapshot.captured_at.desc())
            .limit(1)
        )
        snapshots = list(
            await self.session.scalars(
                select(VideoMetricSnapshot)
                .where(
                    VideoMetricSnapshot.bvid == bvid,
                    VideoMetricSnapshot.captured_at >= since_utc,
                    VideoMetricSnapshot.captured_at < until_utc,
                )
                .order_by(VideoMetricSnapshot.captured_at.asc())
                .limit(max_points + 1)
            )
        )
        if len(snapshots) > max_points:
            raise ValueError(
                f"Metric replay exceeds max_points={max_points}; narrow the window"
            )

        points: list[VideoMetricReplayPoint] = []
        for snapshot in snapshots:
            metrics = {field: getattr(snapshot, field) for field in _METRIC_FIELDS}
            deltas: dict[str, int] = {}
            if previous is not None:
                for field, current_value in metrics.items():
                    previous_value = getattr(previous, field)
                    if current_value is not None and previous_value is not None:
                        deltas[field] = current_value - previous_value
            points.append(
                VideoMetricReplayPoint(
                    bvid=bvid,
                    captured_at=snapshot.captured_at,
                    previous_at=(
                        previous.captured_at if previous is not None else None
                    ),
                    elapsed_seconds=(
                        int(
                            (
                                snapshot.captured_at - previous.captured_at
                            ).total_seconds()
                        )
                        if previous is not None
                        else None
                    ),
                    metrics=metrics,
                    deltas=deltas,
                    raw_payload_id=snapshot.raw_payload_id,
                    previous_raw_payload_id=(
                        previous.raw_payload_id if previous is not None else None
                    ),
                )
            )
            previous = snapshot
        return points


@dataclass(frozen=True, slots=True)
class HotCommentReplaySnapshot:
    bvid: str
    captured_at: datetime
    raw_page_observation_id: int
    raw_payload_id: int
    top_n: int
    comments: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "hot-comment-replay-v1",
            "bvid": self.bvid,
            "captured_at": self.captured_at.isoformat(),
            "raw_page_observation_id": self.raw_page_observation_id,
            "raw_payload_id": self.raw_payload_id,
            "top_n": self.top_n,
            "comments": list(self.comments),
        }


class HotCommentReplayAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        *,
        bvid: str,
        since: datetime,
        until: datetime,
        top_n: int = 20,
        max_snapshots: int = 10_000,
    ) -> list[HotCommentReplaySnapshot]:
        since_utc = _aware_utc(since, "since")
        until_utc = _aware_utc(until, "until")
        if until_utc <= since_utc:
            raise ValueError("until must be after since")
        if not 1 <= top_n <= 20:
            raise ValueError("top_n must be between 1 and 20")
        if not 1 <= max_snapshots <= 100_000:
            raise ValueError("max_snapshots must be between 1 and 100000")

        pages = list(
            await self.session.scalars(
                select(RawPageObservation)
                .where(
                    RawPageObservation.request_type == BilibiliRequestType.COMMENT_HOT,
                    RawPageObservation.target_type == "video",
                    RawPageObservation.target_id == bvid,
                    RawPageObservation.sort_mode == "hot",
                    RawPageObservation.page_number == 1,
                    RawPageObservation.status == "success",
                    RawPageObservation.captured_at >= since_utc,
                    RawPageObservation.captured_at < until_utc,
                )
                .order_by(
                    RawPageObservation.captured_at.asc(),
                    RawPageObservation.id.asc(),
                )
                .limit(max_snapshots + 1)
            )
        )
        if len(pages) > max_snapshots:
            raise ValueError(
                f"Hot replay exceeds max_snapshots={max_snapshots}; narrow the window"
            )
        if not pages:
            return []

        page_ids = [page.id for page in pages]
        observations = list(
            await self.session.scalars(
                select(CommentObservation)
                .where(
                    CommentObservation.raw_page_observation_id.in_(page_ids),
                    CommentObservation.sort_mode == "hot",
                    CommentObservation.position.is_not(None),
                    CommentObservation.position <= top_n,
                )
                .order_by(
                    CommentObservation.raw_page_observation_id.asc(),
                    CommentObservation.position.asc(),
                    CommentObservation.rpid.asc(),
                )
            )
        )
        observation_ids = [observation.id for observation in observations]
        media_rows: list[CommentObservationMedia] = []
        if observation_ids:
            media_rows = list(
                await self.session.scalars(
                    select(CommentObservationMedia)
                    .where(
                        CommentObservationMedia.comment_observation_id.in_(
                            observation_ids
                        )
                    )
                    .order_by(
                        CommentObservationMedia.comment_observation_id.asc(),
                        CommentObservationMedia.position.asc(),
                    )
                )
            )
        media_by_observation: dict[int, list[dict[str, Any]]] = {}
        for media in media_rows:
            media_by_observation.setdefault(media.comment_observation_id, []).append(
                {
                    "position": media.position,
                    "role": media.role,
                    "media_source_id": media.media_source_id,
                    "media_asset_id": media.media_asset_id,
                }
            )
        comments_by_page: dict[int, list[dict[str, Any]]] = {
            page_id: [] for page_id in page_ids
        }
        for observation in observations:
            comments_by_page[observation.raw_page_observation_id].append(
                {
                    "position": observation.position,
                    "rpid": observation.rpid,
                    "content": observation.content,
                    "content_hash": observation.content_hash.hex(),
                    "like_count": observation.like_count,
                    "reply_count": observation.reply_count,
                    "author_mid": observation.author_mid,
                    "author_name": observation.author_name,
                    "visibility": observation.visibility,
                    "is_deleted": observation.is_deleted,
                    "comment_observation_id": observation.id,
                    "raw_payload_id": observation.raw_payload_id,
                    "media_ordered_hash": (
                        observation.media_ordered_hash.hex()
                        if observation.media_ordered_hash is not None
                        else None
                    ),
                    "media_set_hash": (
                        observation.media_set_hash.hex()
                        if observation.media_set_hash is not None
                        else None
                    ),
                    "media": media_by_observation.get(observation.id, []),
                }
            )
        return [
            HotCommentReplaySnapshot(
                bvid=bvid,
                captured_at=page.captured_at,
                raw_page_observation_id=page.id,
                raw_payload_id=page.raw_payload_id,
                top_n=top_n,
                comments=tuple(comments_by_page[page.id]),
            )
            for page in pages
        ]


@dataclass(frozen=True, slots=True)
class CommentVisibilityReplayEvent:
    event_id: int
    bvid: str
    rpid: int
    event_type: str
    occurred_at: datetime
    old_visibility: str | None
    new_visibility: str | None
    missing_reason: str | None
    previous_observation: dict[str, Any] | None
    current_observation: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "comment-visibility-replay-v1",
            "event_id": self.event_id,
            "bvid": self.bvid,
            "rpid": self.rpid,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "old_visibility": self.old_visibility,
            "new_visibility": self.new_visibility,
            "missing_reason": self.missing_reason,
            "previous_observation": self.previous_observation,
            "current_observation": self.current_observation,
            "interpretation_limit": (
                "recorded_visibility_transition_not_platform_deletion_proof"
            ),
        }


class CommentVisibilityReplayAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        *,
        bvid: str,
        since: datetime,
        until: datetime,
        max_events: int = 100_000,
    ) -> list[CommentVisibilityReplayEvent]:
        since_utc = _aware_utc(since, "since")
        until_utc = _aware_utc(until, "until")
        if until_utc <= since_utc:
            raise ValueError("until must be after since")
        if not 1 <= max_events <= 1_000_000:
            raise ValueError("max_events must be between 1 and 1000000")
        events = list(
            await self.session.scalars(
                select(CommentVisibilityEvent)
                .where(
                    CommentVisibilityEvent.bvid == bvid,
                    CommentVisibilityEvent.created_at >= since_utc,
                    CommentVisibilityEvent.created_at < until_utc,
                )
                .order_by(
                    CommentVisibilityEvent.created_at.asc(),
                    CommentVisibilityEvent.id.asc(),
                )
                .limit(max_events + 1)
            )
        )
        if len(events) > max_events:
            raise ValueError(
                f"Visibility replay exceeds max_events={max_events}; narrow the window"
            )
        observation_ids = {
            observation_id
            for event in events
            for observation_id in (
                event.previous_comment_observation_id,
                event.current_comment_observation_id,
            )
            if observation_id is not None
        }
        observations: dict[int, CommentObservation] = {}
        if observation_ids:
            rows = await self.session.scalars(
                select(CommentObservation).where(
                    CommentObservation.id.in_(observation_ids)
                )
            )
            observations = {row.id: row for row in rows}
        return [
            CommentVisibilityReplayEvent(
                event_id=event.id,
                bvid=event.bvid,
                rpid=event.rpid,
                event_type=event.event_type,
                occurred_at=event.created_at,
                old_visibility=event.old_visibility,
                new_visibility=event.new_visibility,
                missing_reason=event.missing_reason,
                previous_observation=_observation_evidence(
                    observations.get(event.previous_comment_observation_id)
                ),
                current_observation=_observation_evidence(
                    observations.get(event.current_comment_observation_id)
                ),
            )
            for event in events
        ]


def _observation_evidence(
    observation: CommentObservation | None,
) -> dict[str, Any] | None:
    if observation is None:
        return None
    return {
        "comment_observation_id": observation.id,
        "captured_at": observation.captured_at.isoformat(),
        "content": observation.content,
        "content_hash": observation.content_hash.hex(),
        "author_mid": observation.author_mid,
        "author_name": observation.author_name,
        "visibility": observation.visibility,
        "is_deleted": observation.is_deleted,
        "like_count": observation.like_count,
        "reply_count": observation.reply_count,
        "raw_payload_id": observation.raw_payload_id,
        "raw_page_observation_id": observation.raw_page_observation_id,
        "media_ordered_hash": (
            observation.media_ordered_hash.hex()
            if observation.media_ordered_hash is not None
            else None
        ),
        "media_set_hash": (
            observation.media_set_hash.hex()
            if observation.media_set_hash is not None
            else None
        ),
    }


@dataclass(frozen=True, slots=True)
class EventPropagationReplayRecord:
    event_id: int
    event_slug: str
    record_type: str
    occurred_at: datetime
    source: dict[str, Any]
    target: dict[str, Any]
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "event-propagation-replay-v1",
            "event_id": self.event_id,
            "event_slug": self.event_slug,
            "record_type": self.record_type,
            "occurred_at": self.occurred_at.isoformat(),
            "source": self.source,
            "target": self.target,
            "evidence": self.evidence,
            "interpretation_limit": ("evidenced_edges_only_not_complete_causal_graph"),
        }


class EventPropagationReplayAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        *,
        event_reference: int | str,
        since: datetime,
        until: datetime,
        max_records: int = 100_000,
    ) -> list[EventPropagationReplayRecord]:
        since_utc = _aware_utc(since, "since")
        until_utc = _aware_utc(until, "until")
        if until_utc <= since_utc:
            raise ValueError("until must be after since")
        if not 1 <= max_records <= 1_000_000:
            raise ValueError("max_records must be between 1 and 1000000")
        event = await EventRepository(self.session).resolve_event(event_reference)
        event_videos = list(
            await self.session.scalars(
                select(EventVideo)
                .where(EventVideo.event_id == event.id)
                .order_by(EventVideo.first_seen_at.asc(), EventVideo.bvid.asc())
            )
        )
        bvids = [video.bvid for video in event_videos]
        records = [
            EventPropagationReplayRecord(
                event_id=event.id,
                event_slug=event.slug,
                record_type="video_associated",
                occurred_at=video.first_seen_at,
                source={"event_id": event.id, "event_slug": event.slug},
                target={"bvid": video.bvid},
                evidence={
                    "association_reason": video.association_reason,
                    "source_target_id": video.source_target_id,
                    "confidence": video.confidence,
                },
            )
            for video in event_videos
            if since_utc <= video.first_seen_at < until_utc
        ]
        if not bvids:
            return records

        replies = list(
            await self.session.scalars(
                select(CommentEntity)
                .where(
                    CommentEntity.bvid.in_(bvids),
                    CommentEntity.first_seen_at >= since_utc,
                    CommentEntity.first_seen_at < until_utc,
                    CommentEntity.root_rpid.is_not(None),
                )
                .order_by(CommentEntity.first_seen_at.asc(), CommentEntity.rpid.asc())
                .limit(max_records + 1)
            )
        )
        flags = list(
            await self.session.scalars(
                select(CommentAnalysisFlag)
                .where(
                    CommentAnalysisFlag.event_id == event.id,
                    CommentAnalysisFlag.flag_type == "template_like_comment",
                )
                .order_by(CommentAnalysisFlag.id.asc())
                .limit(max_records + 1)
            )
        )
        entity_ids = {
            rpid
            for reply in replies
            for rpid in (reply.rpid, reply.root_rpid)
            if rpid is not None
        } | {
            rpid
            for flag in flags
            for rpid in (flag.subject_rpid, flag.related_rpid)
            if rpid is not None
        }
        entities: dict[int, CommentEntity] = {}
        if entity_ids:
            rows = await self.session.scalars(
                select(CommentEntity).where(CommentEntity.rpid.in_(entity_ids))
            )
            entities = {row.rpid: row for row in rows}

        for reply in replies:
            root = entities.get(reply.root_rpid)
            if root is None:
                continue
            records.append(
                EventPropagationReplayRecord(
                    event_id=event.id,
                    event_slug=event.slug,
                    record_type="comment_reply",
                    occurred_at=reply.first_seen_at,
                    source=_comment_node(root),
                    target=_comment_node(reply),
                    evidence={
                        "root_rpid": reply.root_rpid,
                        "parent_rpid": reply.parent_rpid,
                        "source_raw_payload_id": root.first_raw_payload_id,
                        "target_raw_payload_id": reply.first_raw_payload_id,
                    },
                )
            )
        for flag in flags:
            source = entities.get(flag.subject_rpid)
            target = entities.get(flag.related_rpid)
            if source is None or target is None:
                continue
            if not since_utc <= target.first_seen_at < until_utc:
                continue
            records.append(
                EventPropagationReplayRecord(
                    event_id=event.id,
                    event_slug=event.slug,
                    record_type="template_propagation",
                    occurred_at=target.first_seen_at,
                    source=_comment_node(source),
                    target=_comment_node(target),
                    evidence={
                        "comment_analysis_flag_id": flag.id,
                        "confidence": flag.confidence,
                        "algorithm": flag.algorithm,
                        "algorithm_version": flag.algorithm_version,
                        "flag_evidence": flag.evidence,
                        "source_raw_payload_id": source.first_raw_payload_id,
                        "target_raw_payload_id": target.first_raw_payload_id,
                    },
                )
            )
        if len(replies) > max_records or len(flags) > max_records:
            raise ValueError(
                f"Propagation replay exceeds max_records={max_records}; "
                "narrow the window"
            )
        if len(records) > max_records:
            raise ValueError(
                f"Propagation replay produces more than max_records={max_records}"
            )
        return sorted(
            records,
            key=lambda row: (row.occurred_at, row.record_type, str(row.target)),
        )


def _comment_node(entity: CommentEntity) -> dict[str, Any]:
    return {
        "rpid": entity.rpid,
        "bvid": entity.bvid,
        "author_mid": entity.author_mid,
        "author_name": entity.author_name,
        "content": entity.first_content,
        "first_seen_at": entity.first_seen_at.isoformat(),
    }


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value.astimezone(UTC)
