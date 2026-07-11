from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import (
    CommentObservation,
    EventKeyword,
    EventVideo,
)
from books_of_time.db.repositories import EventRepository


@dataclass(frozen=True, slots=True)
class KeywordTrendPoint:
    event_id: int
    event_slug: str
    scope_type: str
    scope_id: str
    keyword_id: int
    keyword: str
    normalized_keyword: str
    keyword_version: int
    bucket_start: datetime
    bucket_end: datetime
    distinct_comment_count: int
    observation_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "keyword-trend-v1",
            "event_id": self.event_id,
            "event_slug": self.event_slug,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "keyword_id": self.keyword_id,
            "keyword": self.keyword,
            "normalized_keyword": self.normalized_keyword,
            "keyword_version": self.keyword_version,
            "bucket_start": self.bucket_start.isoformat(),
            "bucket_end": self.bucket_end.isoformat(),
            "distinct_comment_count": self.distinct_comment_count,
            "observation_count": self.observation_count,
        }


@dataclass(frozen=True, slots=True)
class KeywordCooccurrenceEdge:
    event_id: int
    event_slug: str
    scope_type: str
    scope_id: str
    keyword_a_id: int
    keyword_a: str
    keyword_a_version: int
    keyword_b_id: int
    keyword_b: str
    keyword_b_version: int
    distinct_comment_count: int
    observation_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "keyword-cooccurrence-v1",
            "event_id": self.event_id,
            "event_slug": self.event_slug,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "keyword_a_id": self.keyword_a_id,
            "keyword_a": self.keyword_a,
            "keyword_a_version": self.keyword_a_version,
            "keyword_b_id": self.keyword_b_id,
            "keyword_b": self.keyword_b,
            "keyword_b_version": self.keyword_b_version,
            "distinct_comment_count": self.distinct_comment_count,
            "observation_count": self.observation_count,
        }


class KeywordTrendAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        *,
        event_reference: int | str,
        since: datetime,
        until: datetime,
        bucket_seconds: int,
        bvid: str | None = None,
        keyword: str | None = None,
    ) -> list[KeywordTrendPoint]:
        since_utc = _require_aware(since, name="since").astimezone(UTC)
        until_utc = _require_aware(until, name="until").astimezone(UTC)
        if until_utc <= since_utc:
            raise ValueError("until must be after since")
        if bucket_seconds < 60 or bucket_seconds > 86_400:
            raise ValueError("bucket_seconds must be between 60 and 86400")

        bucket_starts = _bucket_starts(since_utc, until_utc, bucket_seconds)
        if len(bucket_starts) > 10_000:
            raise ValueError("Trend query exceeds the 10000 bucket limit")

        event_repository = EventRepository(self.session)
        event = await event_repository.resolve_event(event_reference)
        event_videos = list(
            await self.session.scalars(
                select(EventVideo)
                .where(
                    EventVideo.event_id == event.id,
                    EventVideo.active.is_(True),
                )
                .order_by(EventVideo.bvid.asc())
            )
        )
        associated_bvids = {video.bvid for video in event_videos}
        if bvid is not None and bvid not in associated_bvids:
            raise ValueError(f"Video is not associated with event {event.slug}: {bvid}")
        selected_bvids = [bvid] if bvid is not None else sorted(associated_bvids)

        keywords = _latest_active_keywords(
            list(
                await self.session.scalars(
                    select(EventKeyword)
                    .where(
                        EventKeyword.event_id == event.id,
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
        if keyword is not None:
            normalized_keyword = " ".join(keyword.strip().split()).casefold()
            keywords = [
                item
                for item in keywords
                if item.normalized_keyword == normalized_keyword
            ]
            if not keywords:
                raise ValueError(
                    f"Keyword is not active in event {event.slug}: {normalized_keyword}"
                )
        if not keywords:
            return []

        observations: list[CommentObservation] = []
        if selected_bvids:
            observations = list(
                await self.session.scalars(
                    select(CommentObservation).where(
                        CommentObservation.bvid.in_(selected_bvids),
                        CommentObservation.captured_at >= since_utc,
                        CommentObservation.captured_at < until_utc,
                        CommentObservation.content.is_not(None),
                    )
                )
            )

        distinct: dict[tuple[int, datetime], set[int]] = {}
        observation_counts: dict[tuple[int, datetime], int] = {}
        for observation in observations:
            content = (observation.content or "").casefold()
            bucket_start = _floor_bucket(observation.captured_at, bucket_seconds)
            for keyword in keywords:
                if keyword.normalized_keyword not in content:
                    continue
                key = (keyword.id, bucket_start)
                distinct.setdefault(key, set()).add(observation.rpid)
                observation_counts[key] = observation_counts.get(key, 0) + 1

        scope_type = "video" if bvid is not None else "event"
        scope_id = bvid or event.slug
        points: list[KeywordTrendPoint] = []
        for keyword in keywords:
            for bucket_start in bucket_starts:
                key = (keyword.id, bucket_start)
                points.append(
                    KeywordTrendPoint(
                        event_id=event.id,
                        event_slug=event.slug,
                        scope_type=scope_type,
                        scope_id=scope_id,
                        keyword_id=keyword.id,
                        keyword=keyword.keyword,
                        normalized_keyword=keyword.normalized_keyword,
                        keyword_version=keyword.version,
                        bucket_start=bucket_start,
                        bucket_end=bucket_start + timedelta(seconds=bucket_seconds),
                        distinct_comment_count=len(distinct.get(key, set())),
                        observation_count=observation_counts.get(key, 0),
                    )
                )
        return points


class KeywordCooccurrenceAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        *,
        event_reference: int | str,
        since: datetime,
        until: datetime,
        bvid: str | None = None,
    ) -> list[KeywordCooccurrenceEdge]:
        since_utc = _require_aware(since, name="since").astimezone(UTC)
        until_utc = _require_aware(until, name="until").astimezone(UTC)
        if until_utc <= since_utc:
            raise ValueError("until must be after since")

        event = await EventRepository(self.session).resolve_event(event_reference)
        associated_bvids = set(
            await self.session.scalars(
                select(EventVideo.bvid).where(
                    EventVideo.event_id == event.id,
                    EventVideo.active.is_(True),
                )
            )
        )
        if bvid is not None and bvid not in associated_bvids:
            raise ValueError(f"Video is not associated with event {event.slug}: {bvid}")
        selected_bvids = [bvid] if bvid is not None else sorted(associated_bvids)

        keywords = _latest_active_keywords(
            list(
                await self.session.scalars(
                    select(EventKeyword)
                    .where(
                        EventKeyword.event_id == event.id,
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
        if len(keywords) < 2 or not selected_bvids:
            return []

        observations = list(
            await self.session.scalars(
                select(CommentObservation).where(
                    CommentObservation.bvid.in_(selected_bvids),
                    CommentObservation.captured_at >= since_utc,
                    CommentObservation.captured_at < until_utc,
                    CommentObservation.content.is_not(None),
                )
            )
        )
        distinct: dict[tuple[int, int], set[int]] = {}
        observation_counts: dict[tuple[int, int], int] = {}
        for observation in observations:
            content = (observation.content or "").casefold()
            matched = [
                keyword for keyword in keywords if keyword.normalized_keyword in content
            ]
            for index, keyword_a in enumerate(matched):
                for keyword_b in matched[index + 1 :]:
                    key = (keyword_a.id, keyword_b.id)
                    distinct.setdefault(key, set()).add(observation.rpid)
                    observation_counts[key] = observation_counts.get(key, 0) + 1

        scope_type = "video" if bvid is not None else "event"
        scope_id = bvid or event.slug
        edges: list[KeywordCooccurrenceEdge] = []
        for index, keyword_a in enumerate(keywords):
            for keyword_b in keywords[index + 1 :]:
                key = (keyword_a.id, keyword_b.id)
                observation_count = observation_counts.get(key, 0)
                if observation_count == 0:
                    continue
                edges.append(
                    KeywordCooccurrenceEdge(
                        event_id=event.id,
                        event_slug=event.slug,
                        scope_type=scope_type,
                        scope_id=scope_id,
                        keyword_a_id=keyword_a.id,
                        keyword_a=keyword_a.keyword,
                        keyword_a_version=keyword_a.version,
                        keyword_b_id=keyword_b.id,
                        keyword_b=keyword_b.keyword,
                        keyword_b_version=keyword_b.version,
                        distinct_comment_count=len(distinct[key]),
                        observation_count=observation_count,
                    )
                )
        return edges


def _latest_active_keywords(keywords: list[EventKeyword]) -> list[EventKeyword]:
    latest: list[EventKeyword] = []
    seen: set[str] = set()
    for keyword in keywords:
        if keyword.normalized_keyword in seen:
            continue
        seen.add(keyword.normalized_keyword)
        latest.append(keyword)
    return latest


def _require_aware(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value


def _bucket_starts(
    since: datetime,
    until: datetime,
    bucket_seconds: int,
) -> list[datetime]:
    current = _floor_bucket(since, bucket_seconds)
    starts: list[datetime] = []
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
