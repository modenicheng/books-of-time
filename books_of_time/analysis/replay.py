from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import (
    CommentObservation,
    CommentObservationMedia,
    RawPageObservation,
    VideoMetricSnapshot,
)
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


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value.astimezone(UTC)
