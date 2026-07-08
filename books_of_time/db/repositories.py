from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import (
    CollectionTask,
    CommentEntity,
    CommentObservation,
    FrontierState,
    RawPageObservation,
    RawPayload,
    VideoMetricSnapshot,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.http.client import FetchResult
from books_of_time.parsers.comments import (
    COMMENT_PARSER_VERSION,
    ParsedComment,
    ParsedCommentPage,
)
from books_of_time.parsers.video import ParsedVideoStats
from books_of_time.storage.filesystem import StoredRawPayload


class CollectionTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self,
        *,
        kind: TaskKind,
        target_type: str,
        target_id: str,
        priority: int,
        payload: dict[str, Any],
        not_before: datetime,
        budget_cost: int = 1,
        max_retries: int = 3,
    ) -> CollectionTask:
        task = CollectionTask(
            kind=kind,
            target_type=target_type,
            target_id=target_id,
            priority=priority,
            budget_cost=budget_cost,
            payload=payload,
            not_before=not_before,
            max_retries=max_retries,
            status=TaskStatus.PENDING,
        )
        self.session.add(task)
        await self.session.flush()
        return task

    async def lease_next(
        self,
        *,
        lease_owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> CollectionTask | None:
        stmt = (
            select(CollectionTask)
            .where(
                CollectionTask.status == TaskStatus.PENDING,
                CollectionTask.not_before <= now,
            )
            .order_by(CollectionTask.priority.desc(), CollectionTask.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        task = await self.session.scalar(stmt)
        if task is None:
            return None

        task.status = TaskStatus.RUNNING
        task.lease_owner = lease_owner
        task.lease_until = now + timedelta(seconds=lease_seconds)
        await self.session.flush()
        return task


class VideoMetricSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_from_parsed(
        self,
        parsed: ParsedVideoStats,
    ) -> VideoMetricSnapshot:
        snapshot = VideoMetricSnapshot(
            bvid=parsed.bvid,
            captured_at=parsed.captured_at,
            view_count=parsed.view_count,
            like_count=parsed.like_count,
            coin_count=parsed.coin_count,
            favorite_count=parsed.favorite_count,
            share_count=parsed.share_count,
            reply_count=parsed.reply_count,
            danmaku_count=parsed.danmaku_count,
            raw_payload_id=parsed.raw_payload_id,
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot


class RawPayloadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_from_fetch_result(
        self,
        *,
        result: FetchResult,
        stored: StoredRawPayload,
        parser_version: str | None = None,
    ) -> RawPayload:
        raw = RawPayload(
            captured_at=result.captured_at,
            request_type=result.request_type,
            method=result.method,
            url_hash=hashlib.sha256(result.url.encode()).digest(),
            params_hash=_hash_params(result.params),
            status_code=result.status_code,
            payload_hash=bytes.fromhex(stored.payload_hash_hex),
            storage_uri=stored.storage_uri,
            compressed_size=stored.compressed_size,
            uncompressed_size=stored.uncompressed_size,
            parser_version=parser_version,
            created_at=result.captured_at,
        )
        self.session.add(raw)
        await self.session.flush()
        return raw


class RawPageObservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_from_parsed_page(
        self,
        parsed: ParsedCommentPage,
        *,
        request_type: BilibiliRequestType,
    ) -> RawPageObservation:
        observation = RawPageObservation(
            raw_payload_id=parsed.raw_payload_id,
            captured_at=parsed.captured_at,
            request_type=request_type,
            target_type="video",
            target_id=parsed.bvid,
            page_number=parsed.page_number,
            cursor=parsed.extra.get("request_offset"),
            sort_mode=parsed.sort_mode,
            parser_version=COMMENT_PARSER_VERSION,
            status="success",
            item_count=len(parsed.comments),
            extra=parsed.extra,
        )
        self.session.add(observation)
        await self.session.flush()
        return observation


class CommentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_page(
        self,
        parsed: ParsedCommentPage,
        *,
        raw_page_observation_id: int,
    ) -> list[CommentObservation]:
        observations: list[CommentObservation] = []
        for comment in parsed.comments:
            await self._ensure_entity(
                comment,
                captured_at=parsed.captured_at,
                raw_payload_id=parsed.raw_payload_id,
            )
            observation = CommentObservation(
                rpid=comment.rpid,
                bvid=comment.bvid,
                oid=comment.oid,
                captured_at=parsed.captured_at,
                raw_payload_id=parsed.raw_payload_id,
                raw_page_observation_id=raw_page_observation_id,
                sort_mode=parsed.sort_mode,
                page_number=parsed.page_number,
                position=comment.position,
                content=comment.content,
                content_hash=comment.content_hash,
                like_count=comment.like_count,
                reply_count=comment.reply_count,
                author_mid=comment.author_mid,
                author_name=comment.author_name,
                is_deleted=False,
                visibility="visible",
                extra={},
            )
            self.session.add(observation)
            observations.append(observation)
        await self.session.flush()
        return observations

    async def _ensure_entity(
        self,
        comment: ParsedComment,
        *,
        captured_at: datetime,
        raw_payload_id: int,
    ) -> CommentEntity:
        entity = await self.session.get(CommentEntity, comment.rpid)
        if entity is not None:
            entity.updated_at = captured_at
            return entity

        entity = CommentEntity(
            rpid=comment.rpid,
            oid=comment.oid,
            bvid=comment.bvid,
            root_rpid=comment.root_rpid,
            parent_rpid=comment.parent_rpid,
            author_mid=comment.author_mid,
            author_name=comment.author_name,
            first_content=comment.content,
            first_content_hash=comment.content_hash,
            first_seen_at=captured_at,
            first_raw_payload_id=raw_payload_id,
            created_at=captured_at,
            updated_at=captured_at,
        )
        self.session.add(entity)
        await self.session.flush()
        return entity


class FrontierStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(
        self,
        *,
        target_type: str,
        target_id: str,
        frontier_type: str,
        now: datetime,
    ) -> FrontierState:
        stmt = select(FrontierState).where(
            FrontierState.target_type == target_type,
            FrontierState.target_id == target_id,
            FrontierState.frontier_type == frontier_type,
        )
        state = await self.session.scalar(stmt)
        if state is not None:
            return state

        state = FrontierState(
            target_type=target_type,
            target_id=target_id,
            frontier_type=frontier_type,
            frontier_rpid=None,
            frontier_time=None,
            cursor=None,
            last_scan_at=None,
            last_scan_status=None,
            last_scan_pages=0,
            last_scan_truncated=False,
            extra={},
            created_at=now,
            updated_at=now,
        )
        self.session.add(state)
        await self.session.flush()
        return state

    async def save(self, state: FrontierState) -> FrontierState:
        await self.session.flush()
        return state


def _hash_params(params: dict[str, Any] | None) -> bytes | None:
    if not params:
        return None
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(canonical).digest()
