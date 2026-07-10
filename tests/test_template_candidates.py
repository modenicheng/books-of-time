from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.templates import TemplateCandidateAnalyzer
from books_of_time.db.base import Base
from books_of_time.db.models import CommentEntity, EventVideo
from books_of_time.db.repositories import EventRepository


@pytest.mark.asyncio
async def test_template_candidates_require_similar_cross_video_text_in_window() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(slug="event-a", name="事件 A", now=start)
        for bvid in ("BV1xx411c7mD", "BV1Q541167Qg", "BV17x411w7KC"):
            await repository.attach_video(
                event_id=event.id,
                bvid=bvid,
                association_reason="manual",
                now=start,
            )
        inactive = await session.get(EventVideo, (event.id, "BV17x411w7KC"))
        assert inactive is not None
        inactive.active = False
        session.add_all(
            [
                _comment(
                    1001, "BV1xx411c7mD", 11, "alice", start, "统一回复: 服务器维护中"
                ),
                _comment(
                    1002,
                    "BV1Q541167Qg",
                    22,
                    "bob",
                    start + timedelta(minutes=5),
                    "统一回复, 服务器维护中!",
                ),
                _comment(
                    1003,
                    "BV1xx411c7mD",
                    33,
                    "carol",
                    start + timedelta(minutes=6),
                    "同视频重复内容测试文本",
                ),
                _comment(
                    1007,
                    "BV1xx411c7mD",
                    77,
                    "grace",
                    start + timedelta(minutes=7),
                    "同视频重复内容测试文本",
                ),
                _comment(
                    1004,
                    "BV1Q541167Qg",
                    44,
                    "dave",
                    start + timedelta(hours=2),
                    "统一回复: 服务器维护中",
                ),
                _comment(
                    1005,
                    "BV17x411w7KC",
                    55,
                    "eve",
                    start + timedelta(minutes=4),
                    "统一回复: 服务器维护中",
                ),
                _comment(
                    1006, "BV1Q541167Qg", 66, "frank", start, "完全不同的普通评论"
                ),
            ]
        )
        await session.commit()

        rows = await TemplateCandidateAnalyzer(session).analyze(
            event_reference=event.id,
            since=start,
            until=start + timedelta(hours=3),
            window_seconds=3600,
            min_similarity=0.9,
            min_text_chars=8,
        )

    assert [(row.left_rpid, row.right_rpid) for row in rows] == [(1001, 1002)]
    row = rows[0]
    assert row.similarity == 1.0
    assert row.time_delta_seconds == 300
    assert row.candidate_reason == "normalized_exact_text_cross_video"
    assert row.left_author_mid == 11
    assert row.right_author_name == "bob"
    assert row.left_content == "统一回复: 服务器维护中"
    assert row.left_raw_payload_id == 1001
    assert row.as_dict()["algorithm"] == "sequence_matcher-v1"
    await engine.dispose()


@pytest.mark.asyncio
async def test_template_candidates_validate_limits_and_query_size() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(slug="event-a", name="事件 A", now=now)
        await repository.attach_video(
            event_id=event.id,
            bvid="BV1xx411c7mD",
            association_reason="manual",
            now=now,
        )
        session.add_all(
            [
                _comment(2001, "BV1xx411c7mD", 1, "a", now, "这是第一条足够长的评论"),
                _comment(2002, "BV1xx411c7mD", 2, "b", now, "这是第二条足够长的评论"),
                _comment(2003, "BV1xx411c7mD", 3, "c", now, "这是第三条足够长的评论"),
            ]
        )
        await session.commit()
        analyzer = TemplateCandidateAnalyzer(session)

        with pytest.raises(ValueError, match="min_similarity"):
            await analyzer.analyze(
                event_reference=event.id,
                since=now,
                until=now + timedelta(hours=1),
                window_seconds=3600,
                min_similarity=0.4,
            )
        with pytest.raises(ValueError, match="max_comments"):
            await analyzer.analyze(
                event_reference=event.id,
                since=now,
                until=now + timedelta(hours=1),
                window_seconds=3600,
                max_comments=2,
            )

    await engine.dispose()


def _comment(
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
        first_content_hash=bytes([rpid % 256]) * 32,
        first_seen_at=first_seen_at,
        first_raw_payload_id=rpid,
        created_at=first_seen_at,
        updated_at=first_seen_at,
    )
