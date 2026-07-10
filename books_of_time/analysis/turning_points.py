from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.analysis.hot_turnover import HotCommentTurnoverAnalyzer
from books_of_time.db.models import (
    CommentEntity,
    CommentObservation,
    EventKeyword,
    EventTarget,
    EventVideo,
    VideoInfoSnapshot,
)
from books_of_time.db.repositories import EventRepository


@dataclass(frozen=True, slots=True)
class TurningPointSignal:
    event_id: int
    event_slug: str
    signal_type: str
    detected_at: datetime
    scope_type: str
    scope_id: str
    magnitude: float
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "turning-point-signal-v1",
            "event_id": self.event_id,
            "event_slug": self.event_slug,
            "signal_type": self.signal_type,
            "detected_at": self.detected_at.isoformat(),
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "magnitude": self.magnitude,
            "evidence": self.evidence,
            "algorithm": "adjacent-bucket-event-signals-v1",
            "interpretation_limit": "heuristic_event_signal_not_causal_conclusion",
        }


class TurningPointAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        *,
        event_reference: int | str,
        since: datetime,
        until: datetime,
        bucket_seconds: int = 3600,
        spike_multiplier: float = 3.0,
        min_count: int = 5,
        turnover_threshold: float = 0.5,
        top_n: int = 20,
        max_records: int = 200_000,
    ) -> list[TurningPointSignal]:
        since_utc = _aware_utc(since, "since")
        until_utc = _aware_utc(until, "until")
        if until_utc <= since_utc:
            raise ValueError("until must be after since")
        if not 60 <= bucket_seconds <= 86_400:
            raise ValueError("bucket_seconds must be between 60 and 86400")
        if not 1 < spike_multiplier <= 100:
            raise ValueError("spike_multiplier must be greater than 1 and at most 100")
        if not 1 <= min_count <= 1_000_000:
            raise ValueError("min_count must be between 1 and 1000000")
        if not 0 <= turnover_threshold <= 1:
            raise ValueError("turnover_threshold must be between 0 and 1")
        if not 1 <= top_n <= 20:
            raise ValueError("top_n must be between 1 and 20")
        if not 1 <= max_records <= 2_000_000:
            raise ValueError("max_records must be between 1 and 2000000")

        bucket_starts = _bucket_starts(since_utc, until_utc, bucket_seconds)
        if len(bucket_starts) > 10_000:
            raise ValueError("Turning point query exceeds the 10000 bucket limit")
        event = await EventRepository(self.session).resolve_event(event_reference)
        bvids = list(
            await self.session.scalars(
                select(EventVideo.bvid)
                .where(
                    EventVideo.event_id == event.id,
                    EventVideo.active.is_(True),
                )
                .order_by(EventVideo.bvid.asc())
            )
        )
        if not bvids:
            return []

        entities = list(
            await self.session.scalars(
                select(CommentEntity)
                .where(
                    CommentEntity.bvid.in_(bvids),
                    CommentEntity.first_seen_at >= since_utc,
                    CommentEntity.first_seen_at < until_utc,
                )
                .order_by(CommentEntity.first_seen_at.asc(), CommentEntity.rpid.asc())
                .limit(max_records + 1)
            )
        )
        observations = list(
            await self.session.scalars(
                select(CommentObservation)
                .where(
                    CommentObservation.bvid.in_(bvids),
                    CommentObservation.captured_at >= since_utc,
                    CommentObservation.captured_at < until_utc,
                    CommentObservation.content.is_not(None),
                )
                .order_by(
                    CommentObservation.captured_at.asc(),
                    CommentObservation.id.asc(),
                )
                .limit(max_records + 1)
            )
        )
        if len(entities) > max_records or len(observations) > max_records:
            raise ValueError(
                f"Turning point query exceeds max_records={max_records}; "
                "narrow the window"
            )

        signals = self._comment_spikes(
            event_id=event.id,
            event_slug=event.slug,
            bucket_starts=bucket_starts,
            bucket_seconds=bucket_seconds,
            entities=entities,
            spike_multiplier=spike_multiplier,
            min_count=min_count,
        )
        signals.extend(
            await self._keyword_spikes(
                event_id=event.id,
                event_slug=event.slug,
                bucket_starts=bucket_starts,
                bucket_seconds=bucket_seconds,
                observations=observations,
                spike_multiplier=spike_multiplier,
                min_count=min_count,
            )
        )
        signals.extend(
            await self._hot_turnover_signals(
                event_id=event.id,
                event_slug=event.slug,
                bvids=bvids,
                since=since_utc,
                until=until_utc,
                threshold=turnover_threshold,
                top_n=top_n,
            )
        )
        signals.extend(
            await self._major_creator_signals(
                event_id=event.id,
                event_slug=event.slug,
                bvids=bvids,
                since=since_utc,
                until=until_utc,
            )
        )
        return sorted(
            signals,
            key=lambda row: (row.detected_at, row.signal_type, row.scope_id),
        )

    def _comment_spikes(
        self,
        *,
        event_id: int,
        event_slug: str,
        bucket_starts: list[datetime],
        bucket_seconds: int,
        entities: list[CommentEntity],
        spike_multiplier: float,
        min_count: int,
    ) -> list[TurningPointSignal]:
        by_bucket: dict[datetime, list[CommentEntity]] = defaultdict(list)
        for entity in entities:
            by_bucket[_floor_bucket(entity.first_seen_at, bucket_seconds)].append(
                entity
            )
        signals: list[TurningPointSignal] = []
        for previous_start, current_start in pairwise(bucket_starts):
            previous = by_bucket[previous_start]
            current = by_bucket[current_start]
            if not _is_spike(len(previous), len(current), spike_multiplier, min_count):
                continue
            signals.append(
                TurningPointSignal(
                    event_id=event_id,
                    event_slug=event_slug,
                    signal_type="comment_spike",
                    detected_at=current_start,
                    scope_type="event",
                    scope_id=event_slug,
                    magnitude=round(len(current) / max(len(previous), 1), 6),
                    evidence={
                        "bucket_seconds": bucket_seconds,
                        "previous_bucket_start": previous_start.isoformat(),
                        "current_bucket_start": current_start.isoformat(),
                        "previous_count": len(previous),
                        "current_count": len(current),
                        "current_rpids": [row.rpid for row in current],
                        "current_raw_payload_ids": sorted(
                            {
                                row.first_raw_payload_id
                                for row in current
                                if row.first_raw_payload_id is not None
                            }
                        ),
                        "spike_multiplier": spike_multiplier,
                        "min_count": min_count,
                    },
                )
            )
        return signals

    async def _keyword_spikes(
        self,
        *,
        event_id: int,
        event_slug: str,
        bucket_starts: list[datetime],
        bucket_seconds: int,
        observations: list[CommentObservation],
        spike_multiplier: float,
        min_count: int,
    ) -> list[TurningPointSignal]:
        keywords = _latest_keywords(
            list(
                await self.session.scalars(
                    select(EventKeyword)
                    .where(
                        EventKeyword.event_id == event_id,
                        EventKeyword.active.is_(True),
                    )
                    .order_by(
                        EventKeyword.normalized_keyword.asc(),
                        EventKeyword.version.desc(),
                        EventKeyword.id.desc(),
                    )
                )
            )
        )
        matches: dict[tuple[int, datetime], list[CommentObservation]] = defaultdict(
            list
        )
        for observation in observations:
            content = (observation.content or "").casefold()
            bucket_start = _floor_bucket(observation.captured_at, bucket_seconds)
            for keyword in keywords:
                if keyword.normalized_keyword in content:
                    matches[(keyword.id, bucket_start)].append(observation)

        signals: list[TurningPointSignal] = []
        for keyword in keywords:
            for previous_start, current_start in pairwise(bucket_starts):
                previous = matches[(keyword.id, previous_start)]
                current = matches[(keyword.id, current_start)]
                previous_rpids = {row.rpid for row in previous}
                current_rpids = {row.rpid for row in current}
                if not _is_spike(
                    len(previous_rpids),
                    len(current_rpids),
                    spike_multiplier,
                    min_count,
                ):
                    continue
                signals.append(
                    TurningPointSignal(
                        event_id=event_id,
                        event_slug=event_slug,
                        signal_type="keyword_spike",
                        detected_at=current_start,
                        scope_type="keyword",
                        scope_id=keyword.normalized_keyword,
                        magnitude=round(
                            len(current_rpids) / max(len(previous_rpids), 1), 6
                        ),
                        evidence={
                            "keyword_id": keyword.id,
                            "keyword": keyword.keyword,
                            "keyword_version": keyword.version,
                            "bucket_seconds": bucket_seconds,
                            "previous_bucket_start": previous_start.isoformat(),
                            "current_bucket_start": current_start.isoformat(),
                            "previous_count": len(previous_rpids),
                            "current_count": len(current_rpids),
                            "current_rpids": sorted(current_rpids),
                            "current_observation_ids": [row.id for row in current],
                            "current_raw_payload_ids": sorted(
                                {
                                    row.raw_payload_id
                                    for row in current
                                    if row.raw_payload_id is not None
                                }
                            ),
                            "spike_multiplier": spike_multiplier,
                            "min_count": min_count,
                        },
                    )
                )
        return signals

    async def _hot_turnover_signals(
        self,
        *,
        event_id: int,
        event_slug: str,
        bvids: list[str],
        since: datetime,
        until: datetime,
        threshold: float,
        top_n: int,
    ) -> list[TurningPointSignal]:
        signals: list[TurningPointSignal] = []
        analyzer = HotCommentTurnoverAnalyzer(self.session)
        for bvid in bvids:
            points = await analyzer.analyze(
                bvid=bvid,
                since=since,
                until=until,
                top_n=top_n,
            )
            for point in points:
                if point.turnover_rate < threshold:
                    continue
                signals.append(
                    TurningPointSignal(
                        event_id=event_id,
                        event_slug=event_slug,
                        signal_type="hot_turnover",
                        detected_at=point.current_at,
                        scope_type="video",
                        scope_id=bvid,
                        magnitude=round(point.turnover_rate, 6),
                        evidence=point.as_dict() | {"turnover_threshold": threshold},
                    )
                )
        return signals

    async def _major_creator_signals(
        self,
        *,
        event_id: int,
        event_slug: str,
        bvids: list[str],
        since: datetime,
        until: datetime,
    ) -> list[TurningPointSignal]:
        targets = list(
            await self.session.scalars(
                select(EventTarget).where(
                    EventTarget.event_id == event_id,
                    EventTarget.target_type == "uid",
                    EventTarget.active.is_(True),
                )
            )
        )
        target_by_mid = {
            int(target.normalized_value): target
            for target in targets
            if str(target.extra.get("role", "")).casefold() == "major_creator"
        }
        if not target_by_mid:
            return []
        snapshots = list(
            await self.session.scalars(
                select(VideoInfoSnapshot)
                .where(
                    VideoInfoSnapshot.bvid.in_(bvids),
                    VideoInfoSnapshot.owner_mid.in_(target_by_mid),
                    VideoInfoSnapshot.captured_at < until,
                )
                .order_by(
                    VideoInfoSnapshot.captured_at.asc(),
                    VideoInfoSnapshot.bvid.asc(),
                )
            )
        )
        first_by_video: dict[str, VideoInfoSnapshot] = {}
        for snapshot in snapshots:
            first_by_video.setdefault(snapshot.bvid, snapshot)
        signals: list[TurningPointSignal] = []
        for snapshot in first_by_video.values():
            if snapshot.captured_at < since or snapshot.owner_mid is None:
                continue
            target = target_by_mid[snapshot.owner_mid]
            signals.append(
                TurningPointSignal(
                    event_id=event_id,
                    event_slug=event_slug,
                    signal_type="major_creator_involvement",
                    detected_at=snapshot.captured_at,
                    scope_type="video",
                    scope_id=snapshot.bvid,
                    magnitude=1.0,
                    evidence={
                        "bvid": snapshot.bvid,
                        "title": snapshot.title,
                        "owner_mid": snapshot.owner_mid,
                        "owner_name": snapshot.owner_name,
                        "event_target_id": target.id,
                        "event_target_role": "major_creator",
                        "raw_payload_id": snapshot.raw_payload_id,
                    },
                )
            )
        return signals


def _latest_keywords(keywords: list[EventKeyword]) -> list[EventKeyword]:
    latest: list[EventKeyword] = []
    seen: set[str] = set()
    for keyword in keywords:
        if keyword.normalized_keyword in seen:
            continue
        seen.add(keyword.normalized_keyword)
        latest.append(keyword)
    return latest


def _is_spike(
    previous_count: int,
    current_count: int,
    spike_multiplier: float,
    min_count: int,
) -> bool:
    return current_count >= min_count and current_count >= max(
        min_count,
        previous_count * spike_multiplier,
    )


def _bucket_starts(
    since: datetime,
    until: datetime,
    bucket_seconds: int,
) -> list[datetime]:
    starts: list[datetime] = []
    current = _floor_bucket(since, bucket_seconds)
    while current < until:
        starts.append(current)
        current += timedelta(seconds=bucket_seconds)
    return starts


def _floor_bucket(value: datetime, bucket_seconds: int) -> datetime:
    timestamp = int(value.astimezone(UTC).timestamp())
    return datetime.fromtimestamp(
        timestamp - (timestamp % bucket_seconds),
        tz=UTC,
    )


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value.astimezone(UTC)
