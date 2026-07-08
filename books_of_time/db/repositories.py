from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import CollectionTask, RawPayload, VideoMetricSnapshot
from books_of_time.domain.enums import TaskKind, TaskStatus
from books_of_time.http.client import FetchResult
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


def _hash_params(params: dict[str, Any] | None) -> bytes | None:
    if not params:
        return None
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(canonical).digest()
