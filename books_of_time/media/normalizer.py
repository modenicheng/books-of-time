from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import (
    CollectionTask,
    CommentObservation,
    CommentObservationMedia,
    MediaSource,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind
from books_of_time.parsers.comments import ParsedComment, ParsedCommentPage


class MediaService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register_page_media(
        self,
        *,
        parsed: ParsedCommentPage,
        observations: Sequence[CommentObservation],
        raw_page_id: int,
        platform: str = "bilibili",
    ) -> None:
        observations_by_rpid = {
            observation.rpid: observation for observation in observations
        }
        for comment in parsed.comments:
            observation = observations_by_rpid.get(comment.rpid)
            if observation is None or not comment.media:
                continue

            for media_item in comment.media:
                source = await self._upsert_source(
                    platform=platform,
                    url=media_item.url,
                    captured_at=parsed.captured_at,
                    raw_page_id=raw_page_id,
                )
                await self._insert_observation_link(
                    observation=observation,
                    comment=comment,
                    media_source=source,
                    position=media_item.position,
                    role=media_item.role,
                    raw_page_id=raw_page_id,
                )
                if source.fetch_status == "pending":
                    await self._enqueue_fetch_task(source)

    async def _upsert_source(
        self,
        *,
        platform: str,
        url: str,
        captured_at: datetime,
        raw_page_id: int,
    ) -> MediaSource:
        source_url_hash = _sha256(url)
        normalized_url = normalize_bilibili_image_url(url)
        normalized_url_hash = _sha256(normalized_url)

        source = await self.session.scalar(
            select(MediaSource).where(
                MediaSource.platform == platform,
                MediaSource.source_url_hash == source_url_hash,
            )
        )
        if source is None:
            source = MediaSource(
                platform=platform,
                source_url_hash=source_url_hash,
                source_url=url,
                normalized_url_hash=normalized_url_hash,
                normalized_url=normalized_url,
                media_asset_id=None,
                fetch_status="pending",
                first_seen_at=captured_at,
                last_seen_at=captured_at,
                first_raw_page_id=raw_page_id,
                last_raw_page_id=raw_page_id,
            )
            self.session.add(source)
        else:
            source.last_seen_at = captured_at
            source.last_raw_page_id = raw_page_id

        await self.session.flush()
        return source

    async def _insert_observation_link(
        self,
        *,
        observation: CommentObservation,
        comment: ParsedComment,
        media_source: MediaSource,
        position: int,
        role: str,
        raw_page_id: int,
    ) -> None:
        existing = await self.session.scalar(
            select(CommentObservationMedia).where(
                CommentObservationMedia.comment_observation_id == observation.id,
                CommentObservationMedia.position == position,
            )
        )
        if existing is not None:
            return

        link = CommentObservationMedia(
            comment_observation_id=observation.id,
            bvid=comment.bvid,
            rpid=comment.rpid,
            media_source_id=media_source.id,
            media_asset_id=media_source.media_asset_id,
            position=position,
            role=role,
            raw_page_id=raw_page_id,
            created_at=observation.captured_at,
        )
        self.session.add(link)
        await self.session.flush()

    async def _enqueue_fetch_task(self, source: MediaSource) -> CollectionTask:
        return await CollectionTaskRepository(self.session).enqueue(
            kind=TaskKind.FETCH_MEDIA_ASSET,
            target_type="media_source",
            target_id=str(source.id),
            priority=20,
            payload={
                "media_source_id": source.id,
                "url": source.normalized_url or source.source_url,
                "reason": "comment_media",
            },
            not_before=source.first_seen_at,
            idempotency_key=f"fetch_media_asset:{source.id}",
        )


def normalize_bilibili_image_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return url.strip()
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()
