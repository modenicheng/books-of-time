from __future__ import annotations

import json
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.coverage import CoverageDraft
from books_of_time.db.models import CollectionTask, RawPageObservation
from books_of_time.db.repositories import RawPayloadRepository
from books_of_time.domain.enums import BilibiliRequestType, TaskKind
from books_of_time.http.client import FetchResult
from books_of_time.http.errors import ParseFailure
from books_of_time.parsers.discovery import (
    DISCOVERY_PARSER_VERSION,
    parse_user_video_list,
)
from books_of_time.storage.base import RawPayloadStore
from books_of_time.task_orchestrator.discovery import (
    DiscoveryScheduler,
    EventDiscoveryLink,
    normalize_source_associations,
)


class UserVideosClient(Protocol):
    async def get_user_video_list(self, mid: str, page: int = 1) -> FetchResult: ...


class UserVideosCollector:
    def __init__(
        self,
        *,
        client: UserVideosClient,
        raw_store: RawPayloadStore,
        run_id: str,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.client = client
        self.raw_store = raw_store
        self.run_id = run_id
        self.scheduler = DiscoveryScheduler(session_factory=session_factory)

    async def collect(
        self,
        task: CollectionTask,
        session: AsyncSession,
    ) -> CoverageDraft:
        mid = str(task.payload.get("mid") or task.target_id)
        page = max(int(task.payload.get("page") or 1), 1)
        result = await self.client.get_user_video_list(mid=mid, page=page)
        stored = self.raw_store.save(
            body=result.body,
            captured_at=result.captured_at,
            run_id=self.run_id,
            suffix=".json",
        )
        raw = await RawPayloadRepository(session).insert_from_fetch_result(
            result=result,
            stored=stored,
            parser_version=DISCOVERY_PARSER_VERSION,
        )

        source_associations = normalize_source_associations(
            source_mid=mid,
            source_pool_type=task.payload.get("source_pool_type"),
            source_pool_id=task.payload.get("source_pool_id"),
            source_associations=task.payload.get("source_associations"),
        )

        try:
            videos = parse_user_video_list(
                json.loads(result.body),
                source_mid=mid,
                source_pool_type=task.payload.get("source_pool_type"),
                source_pool_id=task.payload.get("source_pool_id"),
                source_associations=source_associations,
            )
        except Exception as exc:
            raise ParseFailure(
                request_type=result.request_type,
                message=str(exc),
                status_code=result.status_code,
                fetch_result=result,
            ) from exc

        raw_page = RawPageObservation(
            raw_payload_id=raw.id,
            captured_at=result.captured_at,
            request_type=BilibiliRequestType.USER_VIDEO_LIST,
            target_type="user",
            target_id=mid,
            page_number=page,
            cursor=None,
            sort_mode="pubdate",
            parser_version=DISCOVERY_PARSER_VERSION,
            status="success",
            item_count=len(videos),
            extra={
                "source_pool_type": task.payload.get("source_pool_type"),
                "source_pool_id": task.payload.get("source_pool_id"),
                "source_associations": source_associations,
                "reason": task.payload.get("reason"),
                "event_links": task.payload.get("event_links", []),
            },
        )
        session.add(raw_page)
        await session.flush()
        created = await self.scheduler.handle_discovered_videos(
            session=session,
            videos=videos,
            event_links=[
                EventDiscoveryLink(
                    event_id=int(item["event_id"]),
                    target_id=int(item["target_id"]),
                )
                for item in task.payload.get("event_links", [])
            ],
            source_associations=source_associations,
            raw_page_observation_id=raw_page.id,
            now=result.captured_at,
        )
        return CoverageDraft(
            task_kind=TaskKind.DISCOVER_USER_VIDEOS,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=1,
            pages_succeeded=1,
            items_observed=len(videos),
            raw_payloads_saved=1,
            reason="complete",
            extra={"videos_created": len(created)},
        )
