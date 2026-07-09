from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.repositories import VideoMetricSnapshotRepository
from books_of_time.task_orchestrator.snapshot_policy import (
    CoreWindow,
    get_next_snapshot_at,
)


async def get_next_video_snapshot_at(
    session: AsyncSession,
    *,
    bvid: str,
    published_at: datetime,
    now: datetime,
    core_window: CoreWindow | None = None,
) -> datetime | None:
    recent_growth = await VideoMetricSnapshotRepository(session).get_view_growth_since(
        bvid=bvid,
        since=now - timedelta(hours=1),
        now=now,
    )
    return get_next_snapshot_at(
        published_at,
        now,
        recent_view_growth_last_hour=recent_growth,
        core_window=core_window,
    )
