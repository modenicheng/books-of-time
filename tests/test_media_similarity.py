from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.models import (
    Base,
    CollectionTask,
    MediaAsset,
    MediaCluster,
    MediaClusterMember,
    MediaSimilarityEdge,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import TaskKind, TaskStatus
from books_of_time.media.similarity import (
    MediaSimilarityAnalyzer,
    MediaSimilarityCollector,
)
from books_of_time.worker import Worker


def media_asset(
    index: int,
    *,
    phash: int,
) -> MediaAsset:
    return MediaAsset(
        blob_sha256=bytes([index]) * 32,
        pixel_sha256=None,
        mime_type="image/png",
        file_ext=".png",
        width=16,
        height=16,
        size_bytes=128,
        storage_uri=f"file://data/media/{index}.png",
        first_seen_at=datetime(2026, 7, 8, 10, index, tzinfo=UTC),
        first_raw_page_id=10 + index,
        download_raw_payload_id=20 + index,
        phash=phash,
        dhash=None,
        ahash=None,
    )


@pytest.mark.asyncio
async def test_media_similarity_analyzer_creates_edges_and_clusters() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                media_asset(1, phash=0b000000),
                media_asset(2, phash=0b000101),
                media_asset(3, phash=0b111111111111),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        result = await MediaSimilarityAnalyzer().analyze_phash(
            session,
            threshold=5,
        )
        await session.commit()

    async with session_factory() as session:
        edges = (await session.scalars(select(MediaSimilarityEdge))).all()
        clusters = (await session.scalars(select(MediaCluster))).all()
        members = (await session.scalars(select(MediaClusterMember))).all()

        assert result.edges_created == 1
        assert result.clusters_created == 1
        assert len(edges) == 1
        assert edges[0].similarity_type == "phash_hamming"
        assert edges[0].distance == 2
        assert edges[0].confidence > 0.9
        assert len(clusters) == 1
        assert {
            member.media_asset_id
            for member in members
            if member.cluster_id == clusters[0].id
        } == {1, 2}

    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_executes_media_similarity_analysis_task() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    async with session_factory() as session:
        session.add_all(
            [
                media_asset(1, phash=0b000000),
                media_asset(2, phash=0b000001),
            ]
        )
        await session.flush()
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.ANALYZE_SIMILAR_MEDIA,
            target_type="media",
            target_id="phash",
            priority=5,
            payload={"threshold": 5},
            not_before=now,
        )
        await session.commit()

    worker = Worker(
        session_factory=session_factory,
        collectors={
            TaskKind.ANALYZE_SIMILAR_MEDIA: MediaSimilarityCollector(),
        },
        run_id="test-run",
        lease_owner="worker-test",
    )

    executed = await worker.run_once(now=now)
    assert executed is True

    async with session_factory() as session:
        task = await session.scalar(select(CollectionTask))
        edge = await session.scalar(select(MediaSimilarityEdge))

        assert task is not None
        assert task.status == TaskStatus.SUCCEEDED
        assert edge is not None
        assert edge.distance == 1

    await engine.dispose()
