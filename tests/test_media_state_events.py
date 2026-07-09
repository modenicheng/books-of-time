from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.models import Base, CommentObservation, CommentStateEvent
from books_of_time.db.repositories import (
    CommentRepository,
    RawPageObservationRepository,
)
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.media.normalizer import (
    MEDIA_CHANGED,
    MEDIA_ORDER_CHANGED,
    MEDIA_REMOVED,
    MediaService,
)
from books_of_time.parsers.comments import (
    ParsedComment,
    ParsedCommentMedia,
    ParsedCommentPage,
    hash_comment_content,
)


def parsed_page(
    *,
    captured_at: datetime,
    media_urls: list[str],
) -> ParsedCommentPage:
    return ParsedCommentPage(
        bvid="BV1abc",
        oid=777,
        captured_at=captured_at,
        raw_payload_id=int(captured_at.minute),
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
                content="image comment",
                content_hash=hash_comment_content("image comment"),
                like_count=1,
                reply_count=0,
                position=1,
                media=[
                    ParsedCommentMedia(url=url, position=index)
                    for index, url in enumerate(media_urls)
                ],
            )
        ],
        extra={"all_count": 1},
    )


@pytest.mark.asyncio
async def test_media_service_hashes_media_state_and_records_removed_event() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    first = parsed_page(
        captured_at=datetime(2026, 7, 8, 10, 1, tzinfo=UTC),
        media_urls=[
            "https://i0.hdslb.com/bfs/new_dyn/a.jpg",
            "https://i0.hdslb.com/bfs/new_dyn/b.jpg",
        ],
    )
    second = parsed_page(
        captured_at=first.captured_at + timedelta(minutes=1),
        media_urls=["https://i0.hdslb.com/bfs/new_dyn/a.jpg"],
    )

    async with session_factory() as session:
        for page in (first, second):
            raw_page = await RawPageObservationRepository(
                session
            ).insert_from_parsed_page(
                page,
                request_type=BilibiliRequestType.COMMENT_HOT,
            )
            observations = await CommentRepository(session).upsert_page(
                page,
                raw_page_observation_id=raw_page.id,
            )
            await MediaService(session).register_page_media(
                parsed=page,
                observations=observations,
                raw_page_id=raw_page.id,
            )
        await session.commit()

    async with session_factory() as session:
        observations = (
            await session.scalars(
                select(CommentObservation).order_by(CommentObservation.id.asc())
            )
        ).all()
        events = (
            await session.scalars(
                select(CommentStateEvent).order_by(CommentStateEvent.id.asc())
            )
        ).all()

        assert observations[0].media_ordered_hash is not None
        assert observations[0].media_set_hash is not None
        assert observations[1].media_ordered_hash != observations[0].media_ordered_hash
        assert observations[1].media_set_hash != observations[0].media_set_hash
        assert [event.event_type for event in events] == [
            MEDIA_CHANGED,
            MEDIA_REMOVED,
        ]
        assert events[0].old_value["media_source_ids"] == [1, 2]
        assert events[0].new_value["media_source_ids"] == [1]

    await engine.dispose()


@pytest.mark.asyncio
async def test_media_service_records_order_changed_event() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    first = parsed_page(
        captured_at=datetime(2026, 7, 8, 10, 1, tzinfo=UTC),
        media_urls=[
            "https://i0.hdslb.com/bfs/new_dyn/a.jpg",
            "https://i0.hdslb.com/bfs/new_dyn/b.jpg",
        ],
    )
    second = parsed_page(
        captured_at=first.captured_at + timedelta(minutes=1),
        media_urls=[
            "https://i0.hdslb.com/bfs/new_dyn/b.jpg",
            "https://i0.hdslb.com/bfs/new_dyn/a.jpg",
        ],
    )

    async with session_factory() as session:
        for page in (first, second):
            raw_page = await RawPageObservationRepository(
                session
            ).insert_from_parsed_page(
                page,
                request_type=BilibiliRequestType.COMMENT_HOT,
            )
            observations = await CommentRepository(session).upsert_page(
                page,
                raw_page_observation_id=raw_page.id,
            )
            await MediaService(session).register_page_media(
                parsed=page,
                observations=observations,
                raw_page_id=raw_page.id,
            )
        await session.commit()

    async with session_factory() as session:
        observations = (
            await session.scalars(
                select(CommentObservation).order_by(CommentObservation.id.asc())
            )
        ).all()
        events = (await session.scalars(select(CommentStateEvent))).all()

        assert observations[1].media_ordered_hash != observations[0].media_ordered_hash
        assert observations[1].media_set_hash == observations[0].media_set_hash
        assert [event.event_type for event in events] == [MEDIA_ORDER_CHANGED]

    await engine.dispose()
