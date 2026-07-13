from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import (
    CollectionTask,
    CommentEntity,
    CommentObservation,
    HttpRequestAttempt,
    KnownVideo,
    KnownVideoSource,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus


@pytest.mark.asyncio
async def test_collection_evidence_models_round_trip() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)

    async with session_factory() as session:
        session.add(
            KnownVideo(
                bvid="BV-EVIDENCE",
                source_mid="401742377",
                pubdate=now,
                first_seen_at=now,
            )
        )
        source = KnownVideoSource(
            bvid="BV-EVIDENCE",
            source_mid="401742377",
            pool_type="game",
            pool_id="genshin_impact",
            game_id="genshin_impact",
            official=True,
            monitored=True,
            first_seen_at=now,
            last_seen_at=now,
            first_raw_page_id=11,
            last_raw_page_id=11,
            active=True,
            created_at=now,
            updated_at=now,
        )
        task = CollectionTask(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id="BV-EVIDENCE",
            priority=100,
            budget_cost=1,
            status=TaskStatus.RUNNING,
            payload={"bvid": "BV-EVIDENCE"},
            not_before=now,
            retry_count=0,
            max_retries=3,
            created_at=now,
            updated_at=now,
        )
        session.add_all([source, task])
        await session.flush()
        attempt = HttpRequestAttempt(
            collection_task_id=task.id,
            status="started",
            request_type=BilibiliRequestType.VIDEO_STATS,
            attempt_started_at=now,
            request_started_at=now,
            method="GET",
            url_hash=b"u" * 32,
            params_hash=b"p" * 32,
            created_at=now,
        )
        session.add(attempt)
        await session.commit()

    assert source.game_id == "genshin_impact"
    assert source.official is True
    assert source.monitored is True
    assert attempt.status == "started"
    assert attempt.raw_payload_id is None
    assert CommentEntity.__table__.c.platform_created_at.nullable is True
    assert CommentObservation.__table__.c.author_public_metadata_extra.nullable is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_known_video_source_identity_is_unique() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)

    async with session_factory() as session:
        session.add(
            KnownVideo(
                bvid="BV-UNIQUE",
                source_mid="42",
                pubdate=now,
                first_seen_at=now,
            )
        )
        values = {
            "bvid": "BV-UNIQUE",
            "source_mid": "42",
            "pool_type": "matrix",
            "pool_id": "matrix",
            "official": False,
            "monitored": True,
            "first_seen_at": now,
            "last_seen_at": now,
            "active": True,
            "created_at": now,
            "updated_at": now,
        }
        session.add_all([KnownVideoSource(**values), KnownVideoSource(**values)])
        with pytest.raises(IntegrityError):
            await session.commit()

    await engine.dispose()
