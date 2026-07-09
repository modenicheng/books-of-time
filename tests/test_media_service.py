from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.models import (
    Base,
    CollectionTask,
    CommentObservationMedia,
    MediaSource,
)
from books_of_time.db.repositories import (
    CommentRepository,
    RawPageObservationRepository,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.media.normalizer import MediaService
from books_of_time.parsers.comments import (
    ParsedComment,
    ParsedCommentMedia,
    ParsedCommentPage,
    hash_comment_content,
)


@pytest.mark.asyncio
async def test_media_service_registers_comment_media_and_enqueues_fetch_tasks() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    captured_at = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    repeated_url = "https://i0.hdslb.com/bfs/new_dyn/a.jpg"
    second_url = "https://i0.hdslb.com/bfs/new_dyn/b.png"
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
                content_hash=hash_comment_content("first comment"),
                like_count=12,
                reply_count=3,
                position=1,
                media=[
                    ParsedCommentMedia(url=repeated_url, position=0),
                    ParsedCommentMedia(url=second_url, position=1),
                ],
            ),
            ParsedComment(
                rpid=1002,
                oid=777,
                bvid="BV1abc",
                root_rpid=None,
                parent_rpid=None,
                author_mid=84,
                author_name="Bob",
                content="second comment",
                content_hash=hash_comment_content("second comment"),
                like_count=5,
                reply_count=0,
                position=2,
                media=[ParsedCommentMedia(url=repeated_url, position=0)],
            ),
        ],
        extra={"all_count": 2},
    )

    async with session_factory() as session:
        raw_page = await RawPageObservationRepository(session).insert_from_parsed_page(
            parsed,
            request_type=BilibiliRequestType.COMMENT_HOT,
        )
        observations = await CommentRepository(session).upsert_page(
            parsed,
            raw_page_observation_id=raw_page.id,
        )
        await MediaService(session).register_page_media(
            parsed=parsed,
            observations=observations,
            raw_page_id=raw_page.id,
        )
        await session.commit()

    async with session_factory() as session:
        assert await session.scalar(select(func.count(MediaSource.id))) == 2
        assert (
            await session.scalar(select(func.count(CommentObservationMedia.id)))
        ) == 3
        assert (
            await session.scalar(
                select(func.count(CollectionTask.id)).where(
                    CollectionTask.kind == TaskKind.FETCH_MEDIA_ASSET
                )
            )
        ) == 2

        tasks = (
            await session.scalars(
                select(CollectionTask)
                .where(CollectionTask.kind == TaskKind.FETCH_MEDIA_ASSET)
                .order_by(CollectionTask.id)
            )
        ).all()
        assert [task.kind for task in tasks] == [
            TaskKind.FETCH_MEDIA_ASSET,
            TaskKind.FETCH_MEDIA_ASSET,
        ]
        assert [task.status for task in tasks] == [
            TaskStatus.PENDING,
            TaskStatus.PENDING,
        ]
        assert {task.payload["url"] for task in tasks} == {repeated_url, second_url}

        links = (
            await session.scalars(
                select(CommentObservationMedia).order_by(
                    CommentObservationMedia.rpid.asc(),
                    CommentObservationMedia.position.asc(),
                )
            )
        ).all()
        assert [(link.rpid, link.position) for link in links] == [
            (1001, 0),
            (1001, 1),
            (1002, 0),
        ]
        assert links[0].media_source_id == links[2].media_source_id

    await engine.dispose()
