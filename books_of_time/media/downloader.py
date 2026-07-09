from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.coverage import CoverageDraft
from books_of_time.db.models import CommentObservationMedia, MediaAsset, MediaSource
from books_of_time.db.repositories import RawPayloadRepository
from books_of_time.domain.enums import BilibiliRequestType, TaskKind
from books_of_time.http.client import RawHttpClient
from books_of_time.http.errors import RequestFailure
from books_of_time.http.rate_limiter import TokenBucketRateLimiter
from books_of_time.media.hasher import MediaHasher
from books_of_time.media.storage import MediaStore
from books_of_time.storage.filesystem import RawPayloadFileStore


class MediaAssetCollector:
    def __init__(self, downloader: MediaDownloader) -> None:
        self.downloader = downloader

    async def collect(self, task, session: AsyncSession) -> CoverageDraft:
        media_source_id = int(task.payload.get("media_source_id") or task.target_id)
        await self.downloader.fetch_media_source(media_source_id, session)
        return CoverageDraft(
            task_kind=TaskKind.FETCH_MEDIA_ASSET,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=1,
            pages_succeeded=1,
            items_observed=1,
            raw_payloads_saved=1,
            truncated=False,
            reason="complete",
        )


class MediaDownloader:
    def __init__(
        self,
        *,
        http_client: RawHttpClient,
        rate_limiter: TokenBucketRateLimiter | None,
        media_store: MediaStore,
        raw_store: RawPayloadFileStore,
        run_id: str,
        hasher: MediaHasher | None = None,
    ) -> None:
        self.http_client = http_client
        self.rate_limiter = rate_limiter
        self.media_store = media_store
        self.raw_store = raw_store
        self.run_id = run_id
        self.hasher = hasher or MediaHasher()

    async def fetch_media_source(
        self,
        media_source_id: int,
        session: AsyncSession,
    ) -> MediaAsset:
        source = await session.get(MediaSource, media_source_id)
        if source is None:
            raise ValueError(f"Media source does not exist: {media_source_id}")
        if source.media_asset_id is not None and source.fetch_status == "succeeded":
            existing_asset = await session.get(MediaAsset, source.media_asset_id)
            if existing_asset is not None:
                return existing_asset

        url = source.normalized_url or source.source_url
        if not url:
            raise ValueError(f"Media source has no downloadable URL: {media_source_id}")

        await self._acquire_rate_limits()
        try:
            result = await self.http_client.request(
                method="GET",
                url=url,
                request_type=BilibiliRequestType.MEDIA_IMAGE,
                headers={"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
            )
        except RequestFailure as exc:
            source.fetch_status = "failed"
            source.fetch_error_type = exc.kind.value
            source.fetch_error_message = str(exc)
            await session.flush()
            raise

        mime_type = _content_type(result.response_headers)
        file_ext = _file_ext(mime_type=mime_type, url=result.url)
        stored_raw = self.raw_store.save(
            body=result.body,
            captured_at=result.captured_at,
            run_id=self.run_id,
            suffix=file_ext,
        )
        raw_payload = await RawPayloadRepository(session).insert_from_fetch_result(
            result=result,
            stored=stored_raw,
        )

        blob_sha256 = self.hasher.blob_sha256(result.body)
        asset = await session.scalar(
            select(MediaAsset).where(MediaAsset.blob_sha256 == blob_sha256)
        )
        if asset is None:
            storage_uri = await self.media_store.put(blob_sha256, result.body, file_ext)
            asset = MediaAsset(
                blob_sha256=blob_sha256,
                pixel_sha256=None,
                mime_type=mime_type,
                file_ext=file_ext,
                width=None,
                height=None,
                size_bytes=len(result.body),
                storage_uri=storage_uri,
                first_seen_at=source.first_seen_at,
                first_raw_page_id=source.first_raw_page_id,
                download_raw_payload_id=raw_payload.id,
                phash=None,
                dhash=None,
                ahash=None,
            )
            session.add(asset)
            await session.flush()

        await self._attach_asset(source, asset, session)
        return asset

    async def _attach_asset(
        self,
        source: MediaSource,
        asset: MediaAsset,
        session: AsyncSession,
    ) -> None:
        source.media_asset_id = asset.id
        source.fetch_status = "succeeded"
        source.fetch_error_type = None
        source.fetch_error_message = None
        await session.execute(
            update(CommentObservationMedia)
            .where(CommentObservationMedia.media_source_id == source.id)
            .values(media_asset_id=asset.id)
        )
        await session.flush()

    async def _acquire_rate_limits(self) -> None:
        if self.rate_limiter is None:
            return
        await self.rate_limiter.acquire("global")
        await self.rate_limiter.acquire("host:bilibili")
        await self.rate_limiter.acquire(BilibiliRequestType.MEDIA_IMAGE.value)


def _content_type(headers: dict[str, str] | None) -> str | None:
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == "content-type":
            return value.split(";", 1)[0].strip().lower() or None
    return None


def _file_ext(*, mime_type: str | None, url: str) -> str:
    if mime_type == "image/jpeg":
        return ".jpg"
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type)
        if guessed:
            return guessed

    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    if suffix:
        return suffix
    return ".bin"
