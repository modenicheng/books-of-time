from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.hot_turnover import HotCommentTurnoverAnalyzer
from books_of_time.db.base import Base
from books_of_time.db.models import CommentObservation, RawPageObservation
from books_of_time.domain.enums import BilibiliRequestType


@pytest.mark.asyncio
async def test_hot_turnover_compares_consecutive_first_page_snapshots() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    bvid = "BV1xx411c7mD"

    async with session_factory() as session:
        pages = [
            _page(1, bvid, start, page_number=1),
            _page(2, bvid, start + timedelta(minutes=1), page_number=2),
            _page(3, bvid, start + timedelta(minutes=10), page_number=1),
            _page(4, bvid, start + timedelta(minutes=20), page_number=1),
            _page(5, bvid, start + timedelta(minutes=30), page_number=1),
            _page(6, bvid, start + timedelta(minutes=40), page_number=1),
        ]
        session.add_all(pages)
        session.add_all(
            [
                *_snapshot(1, bvid, start, [101, 102, 103]),
                *_snapshot(2, bvid, start + timedelta(minutes=1), [999]),
                *_snapshot(3, bvid, start + timedelta(minutes=10), [102, 103, 104]),
                *_snapshot(4, bvid, start + timedelta(minutes=20), [103, 104, 105]),
                *_snapshot(6, bvid, start + timedelta(minutes=40), [106]),
            ]
        )
        await session.commit()

        points = await HotCommentTurnoverAnalyzer(session).analyze(
            bvid=bvid,
            since=start,
            until=start + timedelta(hours=1),
            top_n=3,
        )

    assert len(points) == 4
    assert points[0].previous_raw_page_id == 1
    assert points[0].current_raw_page_id == 3
    assert points[0].retained_count == 2
    assert points[0].entered_rpids == (104,)
    assert points[0].exited_rpids == (101,)
    assert points[0].turnover_rate == pytest.approx(1 / 3)
    assert points[1].entered_rpids == (105,)
    assert points[1].exited_rpids == (102,)
    assert points[1].as_dict()["schema_version"] == "hot-comment-turnover-v1"
    assert points[2].current_rpids == ()
    assert points[2].turnover_rate == 1
    assert points[3].previous_rpids == ()
    assert points[3].entered_rpids == (106,)
    assert points[3].turnover_rate == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_hot_turnover_requires_valid_window_and_first_page_top_n() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 7, 10, tzinfo=UTC)

    async with session_factory() as session:
        analyzer = HotCommentTurnoverAnalyzer(session)
        with pytest.raises(ValueError, match="until"):
            await analyzer.analyze(
                bvid="BV1xx411c7mD",
                since=now,
                until=now,
                top_n=10,
            )
        with pytest.raises(ValueError, match="top_n"):
            await analyzer.analyze(
                bvid="BV1xx411c7mD",
                since=now,
                until=now + timedelta(hours=1),
                top_n=21,
            )

    await engine.dispose()


def _page(
    page_id: int,
    bvid: str,
    captured_at: datetime,
    *,
    page_number: int,
) -> RawPageObservation:
    return RawPageObservation(
        id=page_id,
        raw_payload_id=page_id,
        captured_at=captured_at,
        request_type=BilibiliRequestType.COMMENT_HOT,
        target_type="video",
        target_id=bvid,
        page_number=page_number,
        cursor=None,
        sort_mode="hot",
        parser_version="comments.v1",
        status="success",
        item_count=3,
        extra={},
    )


def _snapshot(
    raw_page_id: int,
    bvid: str,
    captured_at: datetime,
    rpids: list[int],
) -> list[CommentObservation]:
    return [
        CommentObservation(
            id=(raw_page_id * 100) + position,
            rpid=rpid,
            bvid=bvid,
            oid=777,
            captured_at=captured_at,
            raw_payload_id=raw_page_id,
            raw_page_observation_id=raw_page_id,
            sort_mode="hot",
            page_number=1,
            position=position,
            content=f"comment-{rpid}",
            content_hash=bytes([position]) * 32,
            like_count=position,
            reply_count=0,
            author_mid=rpid,
            author_name=f"user-{rpid}",
            is_deleted=False,
            visibility="visible",
            extra={},
        )
        for position, rpid in enumerate(rpids, start=1)
    ]
