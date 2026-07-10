from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.comment_flags import CommentFlagAnalyzer
from books_of_time.db.base import Base
from books_of_time.db.models import (
    CommentAnalysisFlag,
    CommentEntity,
    CommentObservation,
)
from books_of_time.db.repositories import EventRepository


@pytest.mark.asyncio
async def test_comment_flag_analyzer_persists_three_idempotent_flag_types() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(slug="event-a", name="事件 A", now=start)
        for bvid in ("BV1xx411c7mD", "BV1Q541167Qg"):
            await repository.attach_video(
                event_id=event.id,
                bvid=bvid,
                association_reason="manual",
                now=start,
            )
        session.add_all(
            [
                _entity(
                    1001, "BV1xx411c7mD", 11, "alice", start, "相同作者重复提交内容甲"
                ),
                _entity(
                    1002,
                    "BV1Q541167Qg",
                    11,
                    "alice",
                    start + timedelta(minutes=2),
                    "相同作者重复提交内容甲",
                ),
                _entity(
                    1003, "BV1xx411c7mD", 22, "bob", start, "统一回复: 服务器维护乙"
                ),
                _entity(
                    1004,
                    "BV1Q541167Qg",
                    33,
                    "carol",
                    start + timedelta(minutes=3),
                    "统一回复, 服务器维护乙!",
                ),
                _observation(1, 1001, "BV1xx411c7mD", start, raw_page_id=501),
                _observation(2, 1001, "BV1xx411c7mD", start, raw_page_id=501),
            ]
        )
        await session.commit()

        analyzer = CommentFlagAnalyzer(session)
        first = await analyzer.refresh(
            event_reference=event.id,
            since=start,
            until=start + timedelta(hours=1),
            detected_at=start + timedelta(hours=1),
            template_window_seconds=3600,
            template_min_similarity=0.9,
            template_min_text_chars=8,
        )
        await session.commit()
        second = await analyzer.refresh(
            event_reference="event-a",
            since=start,
            until=start + timedelta(hours=1),
            detected_at=start + timedelta(hours=2),
            template_window_seconds=3600,
            template_min_similarity=0.9,
            template_min_text_chars=8,
        )
        await session.commit()

        stored = list(
            await session.scalars(
                select(CommentAnalysisFlag).order_by(
                    CommentAnalysisFlag.flag_type,
                    CommentAnalysisFlag.subject_rpid,
                    CommentAnalysisFlag.related_rpid,
                )
            )
        )
        count = await session.scalar(select(func.count(CommentAnalysisFlag.id)))

    assert first.created_count == 4
    assert first.matched_count == 4
    assert second.created_count == 0
    assert second.matched_count == 4
    assert count == 4
    assert {flag.flag_type for flag in stored} == {
        "same_rpid_duplicate_display",
        "same_user_duplicate_submission",
        "template_like_comment",
    }
    duplicate_display = next(
        flag for flag in stored if flag.flag_type == "same_rpid_duplicate_display"
    )
    assert duplicate_display.subject_rpid == 1001
    assert duplicate_display.related_rpid is None
    assert duplicate_display.evidence["raw_page_observation_id"] == 501
    same_user = next(
        flag for flag in stored if flag.flag_type == "same_user_duplicate_submission"
    )
    assert (same_user.subject_rpid, same_user.related_rpid) == (1001, 1002)
    assert same_user.evidence["author_mid"] == 11
    assert same_user.confidence == 1.0
    assert len({flag.stable_key for flag in stored}) == 4
    await engine.dispose()


def _entity(
    rpid: int,
    bvid: str,
    author_mid: int,
    author_name: str,
    first_seen_at: datetime,
    content: str,
) -> CommentEntity:
    return CommentEntity(
        rpid=rpid,
        oid=777,
        bvid=bvid,
        root_rpid=None,
        parent_rpid=None,
        author_mid=author_mid,
        author_name=author_name,
        first_content=content,
        first_content_hash=sha256(content.encode()).digest(),
        first_seen_at=first_seen_at,
        first_raw_payload_id=rpid,
        created_at=first_seen_at,
        updated_at=first_seen_at,
    )


def _observation(
    observation_id: int,
    rpid: int,
    bvid: str,
    captured_at: datetime,
    *,
    raw_page_id: int,
) -> CommentObservation:
    return CommentObservation(
        id=observation_id,
        rpid=rpid,
        bvid=bvid,
        oid=777,
        captured_at=captured_at,
        raw_payload_id=observation_id,
        raw_page_observation_id=raw_page_id,
        sort_mode="hot",
        page_number=1,
        position=1,
        content="相同作者重复提交内容甲",
        content_hash=b"x" * 32,
        like_count=1,
        reply_count=0,
        author_mid=11,
        author_name="alice",
        is_deleted=False,
        visibility="visible",
        extra={},
    )
