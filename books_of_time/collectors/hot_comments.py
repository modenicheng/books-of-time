from __future__ import annotations

import json
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.coverage import CoverageDraft
from books_of_time.db.models import CollectionTask, RawPayload
from books_of_time.db.repositories import (
    CommentRepository,
    RawPageObservationRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind
from books_of_time.domain.watchlist import WatchlistPolicy
from books_of_time.http.client import FetchResult
from books_of_time.http.errors import ParseFailure
from books_of_time.media.normalizer import MediaService
from books_of_time.parsers.comments import (
    COMMENT_PARSER_VERSION,
    ParsedCommentPage,
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
        watchlist_policy: WatchlistPolicy | None = None,
    ) -> None:
        self.client = client
        self.raw_store = raw_store
        self.run_id = run_id
        self.watchlist_policy = watchlist_policy or WatchlistPolicy()

    async def collect(
        self,
        task: CollectionTask,
        session: AsyncSession,
    ) -> CoverageDraft:
        bvid = str(task.payload.get("bvid") or task.target_id)
        start_page = int(task.payload.get("page") or 1)
        page_limit = max(int(task.payload.get("page_limit") or 1), 1)
        aid = task.payload.get("aid")
        raw_payloads_saved = 0
        pages_succeeded = 0
        items_observed = 0

        if aid is None:
            video_result = await self.client.get_video_stats(bvid)
            video_raw = await self._archive_raw(video_result, session)
            raw_payloads_saved += 1
            try:
                video_payload = json.loads(video_result.body)
                aid = _extract_aid(video_payload)
            except Exception as exc:
                raise ParseFailure(
                    request_type=video_result.request_type,
                    message=str(exc),
                    status_code=video_result.status_code,
                    fetch_result=video_result,
                ) from exc
            task.payload = {
                **task.payload,
                "aid": aid,
                "video_raw_payload_id": video_raw.id,
            }

        for page in range(start_page, start_page + page_limit):
            observations_count = await self._collect_page(
                session=session,
                bvid=bvid,
                aid=int(aid),
                page=page,
            )
            raw_payloads_saved += 1
            pages_succeeded += 1
            items_observed += observations_count

        return CoverageDraft(
            task_kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=page_limit,
            pages_succeeded=pages_succeeded,
            items_observed=items_observed,
            raw_payloads_saved=raw_payloads_saved,
            truncated=False,
            reason="complete",
        )

    async def _collect_page(
        self,
        *,
        session: AsyncSession,
        bvid: str,
        aid: int,
        page: int,
    ) -> int:
        comments_result = await self.client.get_hot_comments(aid=aid, page=page)
        comments_raw = await self._archive_raw(comments_result, session)
        try:
            parsed = self._parse_page(
                comments_result,
                bvid=bvid,
                aid=aid,
                page=page,
                raw_payload_id=comments_raw.id,
            )
        except Exception as exc:
            raise ParseFailure(
                request_type=comments_result.request_type,
                message=str(exc),
                status_code=comments_result.status_code,
                fetch_result=comments_result,
            ) from exc
        raw_page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_HOT,
        )
        observations = await CommentRepository(
            session,
            watchlist_policy=self.watchlist_policy,
        ).upsert_page(
            parsed,
            raw_page_observation_id=raw_page.id,
        )
        await MediaService(session).register_page_media(
            parsed=parsed,
            observations=observations,
            raw_page_id=raw_page.id,
        )
        return len(observations)

    def _parse_page(
        self,
        result: FetchResult,
        *,
        bvid: str,
        aid: int,
        page: int,
        raw_payload_id: int,
    ) -> ParsedCommentPage:
        return parse_hot_comment_page(
            json.loads(result.body),
            bvid=bvid,
            oid=aid,
            captured_at=result.captured_at,
            raw_payload_id=raw_payload_id,
            page_number=page,
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
