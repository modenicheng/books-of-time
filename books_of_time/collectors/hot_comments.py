from __future__ import annotations

import json
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import CollectionTask, RawPayload
from books_of_time.db.repositories import (
    CommentRepository,
    RawPageObservationRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.client import FetchResult
from books_of_time.parsers.comments import (
    COMMENT_PARSER_VERSION,
    parse_hot_comment_page,
)
from books_of_time.storage.filesystem import RawPayloadFileStore


class HotCommentsClient(Protocol):
    async def get_video_stats(self, bvid: str) -> FetchResult: ...

    async def get_hot_comments(self, *, aid: int, page: int = 1) -> FetchResult: ...


class HotCommentCollector:
    def __init__(
        self,
        *,
        client: HotCommentsClient,
        raw_store: RawPayloadFileStore,
        run_id: str,
    ) -> None:
        self.client = client
        self.raw_store = raw_store
        self.run_id = run_id

    async def collect(self, task: CollectionTask, session: AsyncSession) -> None:
        bvid = str(task.payload.get("bvid") or task.target_id)
        page = int(task.payload.get("page") or 1)
        aid = task.payload.get("aid")

        if aid is None:
            video_result = await self.client.get_video_stats(bvid)
            video_raw = await self._archive_raw(video_result, session)
            video_payload = json.loads(video_result.body)
            aid = _extract_aid(video_payload)
            task.payload = {
                **task.payload,
                "aid": aid,
                "video_raw_payload_id": video_raw.id,
            }

        comments_result = await self.client.get_hot_comments(aid=int(aid), page=page)
        comments_raw = await self._archive_raw(comments_result, session)
        parsed = parse_hot_comment_page(
            json.loads(comments_result.body),
            bvid=bvid,
            oid=int(aid),
            captured_at=comments_result.captured_at,
            raw_payload_id=comments_raw.id,
            page_number=page,
        )
        raw_page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_HOT,
        )
        await CommentRepository(session).upsert_page(
            parsed,
            raw_page_observation_id=raw_page.id,
        )

    async def _archive_raw(
        self,
        result: FetchResult,
        session: AsyncSession,
    ) -> RawPayload:
        stored = self.raw_store.save(
            body=result.body,
            captured_at=result.captured_at,
            run_id=self.run_id,
            suffix=".json",
        )
        return await RawPayloadRepository(session).insert_from_fetch_result(
            result=result,
            stored=stored,
            parser_version=COMMENT_PARSER_VERSION
            if result.request_type == BilibiliRequestType.COMMENT_HOT
            else None,
        )


def _extract_aid(payload: dict) -> int:
    data = payload.get("data") or {}
    aid = data.get("aid")
    if aid is None:
        raise ValueError("Video info payload does not contain data.aid")
    return int(aid)
