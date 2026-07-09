from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import CollectionTask, KnownVideo
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind
from books_of_time.task_orchestrator.video_snapshot_policy import (
    get_next_video_snapshot_at,
)


class VideoSnapshotScheduler:
    async def schedule_next_for_video(
        self,
        *,
        session: AsyncSession,
        bvid: str,
        now: datetime,
    ) -> CollectionTask | None:
        known = await session.scalar(select(KnownVideo).where(KnownVideo.bvid == bvid))
        if known is None:
            return None

        next_at = await get_next_video_snapshot_at(
            session,
            bvid=bvid,
            published_at=known.pubdate,
            now=now,
        )
        if next_at is None:
            return None

        return await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id=bvid,
            priority=80,
            payload={
                "bvid": bvid,
                "reason": "snapshot_policy",
            },
            not_before=next_at,
            idempotency_key=(
                f"{TaskKind.FETCH_VIDEO_STATS.value}:video:{bvid}:"
                f"snapshot:{next_at.isoformat()}"
            ),
        )
