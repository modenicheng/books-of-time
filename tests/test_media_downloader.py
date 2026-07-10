from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.models import (
    Base,
    CollectionTask,
    CommentObservationMedia,
    MediaAsset,
    MediaSource,
    RawPayload,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind, TaskStatus
from books_of_time.http.client import FetchResult
from books_of_time.media.downloader import MediaAssetCollector, MediaDownloader
from books_of_time.media.storage import MediaStore
from books_of_time.storage.filesystem import RawPayloadFileStore
from books_of_time.worker import Worker


class FakeHttpClient:
    def __init__(self, body: bytes, mime_type: str = "image/jpeg") -> None:
        self.body = body
        self.mime_type = mime_type
        self.requests: list[dict] = []

    async def request(self, **kwargs) -> FetchResult:
        self.requests.append(kwargs)
        return FetchResult(
            request_type=kwargs["request_type"],
            method=kwargs["method"],
            url=kwargs["url"],
            params=kwargs.get("params"),
            status_code=200,
            body=self.body,
            captured_at=datetime(2026, 7, 8, 10, 1, tzinfo=UTC),
            response_headers={"content-type": self.mime_type},
        )


class FakeRateLimiter:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def acquire(self, key: str) -> None:
        self.keys.append(key)


def make_image_bytes(
    *,
    fmt: str = "JPEG",
    size: tuple[int, int] = (2, 3),
    color: tuple[int, int, int] = (255, 0, 0),
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_media_downloader_creates_asset_file_and_backfills_links(
    tmp_path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    async with session_factory() as session:
        source = MediaSource(
            platform="bilibili",
            source_url_hash=b"a" * 32,
            source_url="https://i0.hdslb.com/bfs/new_dyn/a.jpg",
            normalized_url_hash=b"b" * 32,
            normalized_url="https://i0.hdslb.com/bfs/new_dyn/a.jpg",
            media_asset_id=None,
            fetch_status="pending",
            first_seen_at=now,
            last_seen_at=now,
            first_raw_page_id=11,
            last_raw_page_id=11,
        )
        session.add(source)
        await session.flush()
        session.add(
            CommentObservationMedia(
                comment_observation_id=22,
                bvid="BV1abc",
                rpid=1001,
                media_source_id=source.id,
                media_asset_id=None,
                position=0,
                role="comment_image",
                raw_page_id=11,
            )
        )
        await session.commit()

    image_body = make_image_bytes(fmt="JPEG", size=(2, 3))
    http_client = FakeHttpClient(image_body)
    rate_limiter = FakeRateLimiter()
    downloader = MediaDownloader(
        http_client=http_client,
        rate_limiter=rate_limiter,
        media_store=MediaStore(tmp_path / "media"),
        raw_store=RawPayloadFileStore(tmp_path / "raw"),
        run_id="test-run",
    )

    async with session_factory() as session:
        asset = await downloader.fetch_media_source(source.id, session)
        await session.commit()

    async with session_factory() as session:
        saved_source = await session.get(MediaSource, source.id)
        link = await session.scalar(select(CommentObservationMedia))
        raw_payload = await session.scalar(select(RawPayload))
        asset_count = await session.scalar(select(func.count(MediaAsset.id)))

        assert asset_count == 1
        assert asset.size_bytes == len(image_body)
        assert asset.mime_type == "image/jpeg"
        assert asset.file_ext == ".jpg"
        assert asset.width == 2
        assert asset.height == 3
        assert asset.pixel_sha256 is not None
        assert asset.phash is not None
        assert asset.dhash is None
        assert asset.ahash is None
        assert asset.storage_uri.startswith(f"file://{tmp_path / 'media'}")
        assert saved_source is not None
        assert saved_source.fetch_status == "succeeded"
        assert saved_source.media_asset_id == asset.id
        assert link is not None
        assert link.media_asset_id == asset.id
        assert raw_payload is not None
        assert raw_payload.request_type.value == "bilibili:media_image"
        assert asset.download_raw_payload_id == raw_payload.id

    assert rate_limiter.keys == ["global", "host:bilibili", "bilibili:media_image"]
    assert http_client.requests[0]["url"] == "https://i0.hdslb.com/bfs/new_dyn/a.jpg"

    await engine.dispose()


@pytest.mark.asyncio
async def test_media_downloader_reuses_existing_asset_for_same_blob(tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    async with session_factory() as session:
        sources = [
            MediaSource(
                platform="bilibili",
                source_url_hash=bytes([index]) * 32,
                source_url=f"https://i0.hdslb.com/bfs/new_dyn/{index}.jpg",
                normalized_url_hash=bytes([index + 10]) * 32,
                normalized_url=f"https://i0.hdslb.com/bfs/new_dyn/{index}.jpg",
                media_asset_id=None,
                fetch_status="pending",
                first_seen_at=now,
                last_seen_at=now,
                first_raw_page_id=11,
                last_raw_page_id=11,
            )
            for index in (1, 2)
        ]
        session.add_all(sources)
        await session.flush()
        source_ids = [source.id for source in sources]
        await session.commit()

    downloader = MediaDownloader(
        http_client=FakeHttpClient(b"same-image"),
        rate_limiter=None,
        media_store=MediaStore(tmp_path / "media"),
        raw_store=RawPayloadFileStore(tmp_path / "raw"),
        run_id="test-run",
    )

    async with session_factory() as session:
        first = await downloader.fetch_media_source(source_ids[0], session)
        second = await downloader.fetch_media_source(source_ids[1], session)
        await session.commit()

    async with session_factory() as session:
        asset_count = await session.scalar(select(func.count(MediaAsset.id)))
        stored_files = list((tmp_path / "media").rglob("*.jpg"))

        assert first.id == second.id
        assert asset_count == 1
        assert len(stored_files) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_executes_fetch_media_asset_task(tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    async with session_factory() as session:
        source = MediaSource(
            platform="bilibili",
            source_url_hash=b"a" * 32,
            source_url="https://i0.hdslb.com/bfs/new_dyn/a.jpg",
            normalized_url_hash=b"b" * 32,
            normalized_url="https://i0.hdslb.com/bfs/new_dyn/a.jpg",
            media_asset_id=None,
            fetch_status="pending",
            first_seen_at=now,
            last_seen_at=now,
            first_raw_page_id=11,
            last_raw_page_id=11,
        )
        session.add(source)
        await session.flush()
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_MEDIA_ASSET,
            target_type="media_source",
            target_id=str(source.id),
            priority=20,
            payload={"media_source_id": source.id},
            not_before=now,
        )
        source_id = source.id
        await session.commit()

    downloader = MediaDownloader(
        http_client=FakeHttpClient(b"worker-image"),
        rate_limiter=None,
        media_store=MediaStore(tmp_path / "media"),
        raw_store=RawPayloadFileStore(tmp_path / "raw"),
        run_id="test-run",
    )
    worker = Worker(
        session_factory=session_factory,
        collectors={TaskKind.FETCH_MEDIA_ASSET: MediaAssetCollector(downloader)},
        run_id="test-run",
        lease_owner="worker-test",
    )

    executed = await worker.run_once(now=now)
    assert executed is True

    async with session_factory() as session:
        task = await session.scalar(select(CollectionTask))
        saved_source = await session.get(MediaSource, source_id)
        asset_count = await session.scalar(select(func.count(MediaAsset.id)))

        assert task is not None
        assert task.status == TaskStatus.SUCCEEDED
        assert saved_source is not None
        assert saved_source.fetch_status == "succeeded"
        assert saved_source.media_asset_id is not None
        assert asset_count == 1

    await engine.dispose()
