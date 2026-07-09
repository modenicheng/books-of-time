from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.db.models import KnownVideo
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind


@dataclass(frozen=True)
class DiscoveredVideo:
    bvid: str
    pubdate: datetime
    source_mid: str
    source_pool_type: str | None = None
    source_pool_id: str | None = None


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
        now: datetime,
    ) -> list[str]:
        created: list[str] = []
        repo = CollectionTaskRepository(session)

        for video in videos:
            existing = await session.scalar(
                select(KnownVideo).where(KnownVideo.bvid == video.bvid)
            )
            if existing is not None:
                continue

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
                },
                not_before=now,
                idempotency_key=(
                    f"{TaskKind.FETCH_VIDEO_STATS.value}:video:{video.bvid}:discovery"
                ),
            )
            created.append(video.bvid)

        return created
