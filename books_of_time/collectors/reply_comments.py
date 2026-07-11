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
    parse_comment_replies_page,
)
from books_of_time.storage.base import RawPayloadStore


class ReplyCommentsClient(Protocol):
    async def get_video_stats(self, bvid: str) -> FetchResult: ...

    async def get_comment_replies(
        self,
        *,
        aid: int,
        root_rpid: int,
        page: int = 1,
        page_size: int = 20,
    ) -> FetchResult: ...


class ReplyCommentCollector:
    def __init__(
        self,
        *,
        client: ReplyCommentsClient,
        raw_store: RawPayloadStore,
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
        root_rpid = int(task.payload.get("root_rpid") or task.target_id)
        start_page = int(task.payload.get("page") or 1)
        page_limit = max(int(task.payload.get("page_limit") or 1), 1)
        page_size = min(max(int(task.payload.get("page_size") or 20), 1), 20)
        aid = task.payload.get("aid")
        raw_payloads_saved = 0
        pages_succeeded = 0
        items_observed = 0

        if aid is None:
            video_result = await self.client.get_video_stats(bvid)
            video_raw = await self._archive_raw(
                video_result,
                session,
                parser_version=None,
            )
            raw_payloads_saved += 1
            try:
                video_payload = json.loads(video_result.body)
                aid = int((video_payload.get("data") or {})["aid"])
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
                root_rpid=root_rpid,
                page=page,
                page_size=page_size,
            )
            raw_payloads_saved += 1
            pages_succeeded += 1
            items_observed += observations_count

        return CoverageDraft(
            task_kind=TaskKind.FETCH_COMMENT_REPLIES,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=page_limit,
            pages_succeeded=pages_succeeded,
            items_observed=items_observed,
            raw_payloads_saved=raw_payloads_saved,
            truncated=False,
            reason="complete",
            extra={
                "reply_roots_requested": 1,
                "reply_roots_succeeded": 1 if pages_succeeded else 0,
                "root_rpid": root_rpid,
            },
        )

    async def _collect_page(
        self,
        *,
        session: AsyncSession,
        bvid: str,
        aid: int,
        root_rpid: int,
        page: int,
        page_size: int,
    ) -> int:
        result = await self.client.get_comment_replies(
            aid=aid,
            root_rpid=root_rpid,
            page=page,
            page_size=page_size,
        )
        raw = await self._archive_raw(
            result,
            session,
            parser_version=COMMENT_PARSER_VERSION,
        )
        try:
            parsed = self._parse_page(
                result,
                bvid=bvid,
                aid=aid,
                root_rpid=root_rpid,
                page=page,
                raw_payload_id=raw.id,
            )
        except Exception as exc:
            raise ParseFailure(
                request_type=result.request_type,
                message=str(exc),
                status_code=result.status_code,
                fetch_result=result,
            ) from exc
        raw_page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_REPLY,
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
        root_rpid: int,
        page: int,
        raw_payload_id: int,
    ) -> ParsedCommentPage:
        return parse_comment_replies_page(
            json.loads(result.body),
            bvid=bvid,
            oid=aid,
            root_rpid=root_rpid,
            captured_at=result.captured_at,
            raw_payload_id=raw_payload_id,
            page_number=page,
        )

    async def _archive_raw(
        self,
        result: FetchResult,
        session: AsyncSession,
        *,
        parser_version: str | None,
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
            parser_version=parser_version,
        )
