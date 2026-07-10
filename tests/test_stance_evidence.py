from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.stance import (
    StanceEvidenceAnalyzer,
    StanceLexicon,
)
from books_of_time.db.base import Base
from books_of_time.db.models import CommentObservation, EventVideo
from books_of_time.db.repositories import EventRepository


def test_stance_lexicon_normalizes_terms_and_rejects_cross_category_duplicates() -> (
    None
):
    lexicon = StanceLexicon.from_config(
        {
            "version": "2026-07-v1",
            "support": [
                "  赞同 ",
                "\uff33\uff35\uff30\uff30\uff2f\uff32\uff34",
                "赞同",
            ],
            "criticism": ["质疑"],
            "neutral": ["求  证"],
        }
    )

    assert lexicon.version == "2026-07-v1"
    assert lexicon.terms["support"] == ("赞同", "support")
    assert lexicon.terms["neutral"] == ("求 证",)

    with pytest.raises(ValueError, match="multiple categories"):
        StanceLexicon.from_config(
            {
                "version": "bad-v1",
                "support": ["说得对"],
                "criticism": ["说得对"],
                "neutral": [],
            }
        )


@pytest.mark.asyncio
async def test_stance_evidence_is_explainable_and_excludes_inactive_videos() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    lexicon = StanceLexicon.from_config(
        {
            "version": "2026-07-v1",
            "support": ["赞同", "说得对"],
            "criticism": ["质疑", "不认同"],
            "neutral": ["求证", "观望"],
        }
    )

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
        inactive = await session.get(EventVideo, (event.id, "BV1Q541167Qg"))
        assert inactive is not None
        inactive.active = False
        session.add_all(
            [
                _observation(1, "BV1xx411c7mD", 1001, start, "赞同, 但也质疑这个数据"),
                _observation(
                    2,
                    "BV1xx411c7mD",
                    1001,
                    start + timedelta(minutes=5),
                    "仍然赞同, 继续求证",
                ),
                _observation(3, "BV1xx411c7mD", 1002, start, "先观望"),
                _observation(4, "BV1Q541167Qg", 1003, start, "失活视频中说得对"),
            ]
        )
        await session.commit()

        rows = await StanceEvidenceAnalyzer(session).analyze(
            event_reference=event.id,
            since=start,
            until=start + timedelta(hours=1),
            lexicon=lexicon,
        )

    by_category = {row.category: row for row in rows}
    assert list(by_category) == ["support", "criticism", "neutral"]
    assert by_category["support"].distinct_comment_count == 1
    assert by_category["support"].observation_count == 2
    assert by_category["support"].matched_term_counts == {"赞同": 2}
    assert by_category["criticism"].distinct_comment_count == 1
    assert by_category["criticism"].matched_term_counts == {"质疑": 1}
    assert by_category["neutral"].distinct_comment_count == 2
    assert by_category["neutral"].observation_count == 2
    assert by_category["neutral"].matched_term_counts == {"求证": 1, "观望": 1}
    assert by_category["support"].as_dict()["lexicon_version"] == "2026-07-v1"
    await engine.dispose()


def _observation(
    observation_id: int,
    bvid: str,
    rpid: int,
    captured_at: datetime,
    content: str,
) -> CommentObservation:
    return CommentObservation(
        id=observation_id,
        rpid=rpid,
        bvid=bvid,
        oid=777,
        captured_at=captured_at,
        raw_payload_id=observation_id,
        raw_page_observation_id=observation_id,
        sort_mode="hot",
        page_number=1,
        position=1,
        content=content,
        content_hash=bytes([observation_id]) * 32,
        like_count=1,
        reply_count=0,
        author_mid=42,
        author_name="user",
        is_deleted=False,
        visibility="visible",
        extra={},
    )
