from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import (
    CollectionTask,
    CommentObservation,
    CommentObservationMedia,
    CommentStateEvent,
    MediaSource,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind
from books_of_time.parsers.comments import ParsedComment, ParsedCommentPage

MEDIA_CHANGED = "media_changed"
MEDIA_ADDED = "media_added"
MEDIA_REMOVED = "media_removed"
MEDIA_ORDER_CHANGED = "media_order_changed"


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

            media_source_ids: list[int] = []
            for media_item in comment.media:
                source = await self._upsert_source(
                    platform=platform,
                    url=media_item.url,
                    captured_at=parsed.captured_at,
                    raw_page_id=raw_page_id,
                )
                media_source_ids.append(source.id)
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
            await self._update_observation_media_state(
                observation,
                media_source_ids=media_source_ids,
            )
            await self._record_media_state_events(
                observation,
                media_source_ids=media_source_ids,
            )

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

    async def _update_observation_media_state(
        self,
        observation: CommentObservation,
        *,
        media_source_ids: list[int],
    ) -> None:
        observation.media_ordered_hash = _hash_media_ids(media_source_ids)
        observation.media_set_hash = _hash_media_ids(sorted(set(media_source_ids)))
        await self.session.flush()

    async def _record_media_state_events(
        self,
        observation: CommentObservation,
        *,
        media_source_ids: list[int],
    ) -> None:
        previous = await self.session.scalar(
            select(CommentObservation)
            .where(
                CommentObservation.rpid == observation.rpid,
                CommentObservation.id < observation.id,
            )
            .order_by(CommentObservation.id.desc())
            .limit(1)
        )
        if previous is None or previous.media_ordered_hash is None:
            return

        previous_media_source_ids = await self._media_source_ids_for_observation(
            previous.id
        )
        if previous_media_source_ids == media_source_ids:
            return

        old_value = {"media_source_ids": previous_media_source_ids}
        new_value = {"media_source_ids": media_source_ids}
        previous_set = set(previous_media_source_ids)
        current_set = set(media_source_ids)
        if previous_set != current_set:
            await self._add_state_event(
                event_type=MEDIA_CHANGED,
                observation=observation,
                previous=previous,
                old_value=old_value,
                new_value=new_value,
            )
            if current_set - previous_set:
                await self._add_state_event(
                    event_type=MEDIA_ADDED,
                    observation=observation,
                    previous=previous,
                    old_value=old_value,
                    new_value=new_value,
                )
            if previous_set - current_set:
                await self._add_state_event(
                    event_type=MEDIA_REMOVED,
                    observation=observation,
                    previous=previous,
                    old_value=old_value,
                    new_value=new_value,
                )
        else:
            await self._add_state_event(
                event_type=MEDIA_ORDER_CHANGED,
                observation=observation,
                previous=previous,
                old_value=old_value,
                new_value=new_value,
            )

    async def _media_source_ids_for_observation(
        self,
        comment_observation_id: int,
    ) -> list[int]:
        rows = await self.session.scalars(
            select(CommentObservationMedia)
            .where(
                CommentObservationMedia.comment_observation_id == comment_observation_id
            )
            .order_by(CommentObservationMedia.position.asc())
        )
        return [row.media_source_id for row in rows]

    async def _add_state_event(
        self,
        *,
        event_type: str,
        observation: CommentObservation,
        previous: CommentObservation,
        old_value: dict[str, list[int]],
        new_value: dict[str, list[int]],
    ) -> None:
        existing = await self.session.scalar(
            select(CommentStateEvent).where(
                CommentStateEvent.current_comment_observation_id == observation.id,
                CommentStateEvent.event_type == event_type,
            )
        )
        if existing is not None:
            return

        self.session.add(
            CommentStateEvent(
                rpid=observation.rpid,
                bvid=observation.bvid,
                previous_comment_observation_id=previous.id,
                current_comment_observation_id=observation.id,
                event_type=event_type,
                old_value=old_value,
                new_value=new_value,
                created_at=observation.captured_at,
            )
        )
        await self.session.flush()


def normalize_bilibili_image_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return url.strip()
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _hash_media_ids(media_ids: list[int]) -> bytes:
    canonical = json.dumps(media_ids, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).digest()
