from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.db.models import KnownVideo
from books_of_time.db.repositories import CollectionTaskRepository, EventRepository
from books_of_time.domain.enums import TaskKind


@dataclass(frozen=True)
class DiscoveredVideo:
    bvid: str
    pubdate: datetime
    source_mid: str
    source_pool_type: str | None = None
    source_pool_id: str | None = None


@dataclass(frozen=True)
class EventDiscoveryLink:
    event_id: int
    target_id: int

    def as_payload(self) -> dict[str, int]:
        return {"event_id": self.event_id, "target_id": self.target_id}


class DiscoveryScheduler:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        fresh_video_window: timedelta = timedelta(minutes=2),
    ) -> None:
        self.session_factory = session_factory
        self.fresh_video_window = fresh_video_window

    async def handle_discovered_videos(
        self,
        *,
        session: AsyncSession,
        videos: list[DiscoveredVideo],
        event_links: list[EventDiscoveryLink] | None = None,
        now: datetime,
    ) -> list[str]:
        created: list[str] = []
        repo = CollectionTaskRepository(session)
        event_repo = EventRepository(session)
        valid_event_links: list[EventDiscoveryLink] = []
        for link in event_links or []:
            target = await event_repo.resolve_active_uid_target(
                event_id=link.event_id,
                target_id=link.target_id,
                now=now,
            )
            if target is not None:
                valid_event_links.append(link)

        for video in videos:
            existing = await session.scalar(
                select(KnownVideo).where(KnownVideo.bvid == video.bvid)
            )
            if existing is None:
                session.add(
                    KnownVideo(
                        bvid=video.bvid,
                        source_mid=video.source_mid,
                        pubdate=video.pubdate,
                        first_seen_at=now,
                    )
                )

                is_delayed_discovery = now - video.pubdate > self.fresh_video_window

                await repo.enqueue(
                    kind=TaskKind.FETCH_VIDEO_STATS,
                    target_type="video",
                    target_id=video.bvid,
                    priority=90 if is_delayed_discovery else 100,
                    payload={
                        "bvid": video.bvid,
                        "source_mid": video.source_mid,
                        "reason": "delayed_discovery"
                        if is_delayed_discovery
                        else "fresh_discovery",
                        "source_pool_type": video.source_pool_type,
                        "source_pool_id": video.source_pool_id,
                        "event_links": [
                            link.as_payload() for link in valid_event_links
                        ],
                    },
                    not_before=now,
                    idempotency_key=(
                        f"{TaskKind.FETCH_VIDEO_STATS.value}:video:"
                        f"{video.bvid}:discovery"
                    ),
                )
                created.append(video.bvid)

            for link in valid_event_links:
                await event_repo.attach_video(
                    event_id=link.event_id,
                    bvid=video.bvid,
                    source_target_id=link.target_id,
                    association_reason="uid_target",
                    confidence=1.0,
                    now=now,
                )

        return created
