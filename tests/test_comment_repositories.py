from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.models import (
    Base,
    CommentEntity,
    CommentObservation,
    RawPageObservation,
)
from books_of_time.db.repositories import (
    CommentRepository,
    FrontierStateRepository,
    RawPageObservationRepository,
)
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.parsers.comments import ParsedComment, ParsedCommentPage


@pytest.mark.asyncio
async def test_comment_repository_upserts_entity_and_appends_observations() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    content_hash = b"x" * 32
    parsed = ParsedCommentPage(
        bvid="BV1abc",
        oid=777,
        captured_at=captured_at,
        raw_payload_id=42,
        sort_mode="hot",
        page_number=1,
        comments=[
            ParsedComment(
                rpid=1001,
                oid=777,
                bvid="BV1abc",
                root_rpid=None,
                parent_rpid=None,
                author_mid=42,
                author_name="Alice",
                content="first comment",
                content_hash=content_hash,
                like_count=12,
                reply_count=3,
                position=1,
            )
        ],
        extra={"all_count": 1},
    )

    async with session_factory() as session:
        page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_HOT,
        )
        await CommentRepository(session).upsert_page(
            parsed,
            raw_page_observation_id=page.id,
        )
        await CommentRepository(session).upsert_page(
            parsed,
            raw_page_observation_id=page.id,
        )
        await session.commit()

    async with session_factory() as session:
        entity_count = await session.scalar(select(func.count(CommentEntity.rpid)))
        observation_count = await session.scalar(
            select(func.count(CommentObservation.id))
        )
        raw_page = await session.scalar(select(RawPageObservation))
        entity = await session.scalar(select(CommentEntity))
        observations = (
            await session.scalars(
                select(CommentObservation).order_by(CommentObservation.id.asc())
            )
        ).all()

        assert entity_count == 1
        assert observation_count == 2
        assert raw_page is not None
        assert raw_page.item_count == 1
        assert raw_page.extra == {"all_count": 1}
        assert entity is not None
        assert entity.rpid == 1001
        assert entity.author_mid == 42
        assert entity.author_name == "Alice"
        assert entity.first_content == "first comment"
        assert entity.first_content_hash == content_hash
        assert observations[0].raw_page_observation_id == raw_page.id
        assert observations[0].content == "first comment"
        assert observations[0].author_name == "Alice"

    await engine.dispose()


@pytest.mark.asyncio
async def test_frontier_repository_creates_once_and_persists_extra() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repo = FrontierStateRepository(session)
        state = await repo.get_or_create(
            target_type="video",
            target_id="BV1abc",
            frontier_type="latest_comments",
            now=now,
        )
        state.extra["baseline_status"] = "baseline_paused"
        state.extra["seen_cursors"] = [""]
        state.cursor = "offset-2"
        await repo.save(state)
        await session.commit()

    async with session_factory() as session:
        repo = FrontierStateRepository(session)
        same = await repo.get_or_create(
            target_type="video",
            target_id="BV1abc",
            frontier_type="latest_comments",
            now=now,
        )

        assert same.id == state.id
        assert same.cursor == "offset-2"
        assert same.extra["baseline_status"] == "baseline_paused"
        assert same.extra["seen_cursors"] == [""]

    await engine.dispose()


@pytest.mark.asyncio
async def test_latest_raw_page_observation_stores_request_cursor() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    parsed = ParsedCommentPage(
        bvid="BV1abc",
        oid=777,
        captured_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
        raw_payload_id=42,
        sort_mode="latest",
        page_number=3,
        comments=[],
        extra={
            "request_offset": "offset-2",
            "next_offset": "offset-3",
            "is_end": False,
        },
    )

    async with session_factory() as session:
        raw_page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_LATEST,
        )
        await session.commit()

    async with session_factory() as session:
        saved = await session.scalar(select(RawPageObservation))

        assert saved is not None
        assert saved.id == raw_page.id
        assert saved.cursor == "offset-2"
        assert saved.sort_mode == "latest"
        assert saved.extra["next_offset"] == "offset-3"

    await engine.dispose()
