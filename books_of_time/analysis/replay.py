from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import VideoMetricSnapshot

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


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value.astimezone(UTC)
