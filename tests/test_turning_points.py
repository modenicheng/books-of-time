from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.turning_points import TurningPointAnalyzer
from books_of_time.db.base import Base
from books_of_time.db.models import (
    CommentEntity,
    CommentObservation,
    RawPageObservation,
    VideoInfoSnapshot,
)
from books_of_time.db.repositories import EventRepository
from books_of_time.domain.enums import BilibiliRequestType


@pytest.mark.asyncio
async def test_turning_points_combine_four_explainable_event_signals() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)

    async with factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(slug="event-a", name="事件 A", now=start)
        await repository.add_target(
            event_id=event.id,
            target_type="keyword",
            target_value="控评",
            now=start,
        )
        await repository.add_target(
            event_id=event.id,
            target_type="uid",
            target_value="999",
            extra={"role": "major_creator"},
            now=start,
        )
        bvid = "BV1xx411c7mD"
        await repository.attach_video(
            event_id=event.id,
            bvid=bvid,
            association_reason="manual",
            now=start,
        )
        entities = [
            _entity(1000 + index, bvid, start + timedelta(minutes=5 * index))
            for index in range(2)
        ] + [
            _entity(
                2000 + index,
                bvid,
                start + timedelta(hours=1, minutes=5 * index),
            )
            for index in range(6)
        ]
        observations = [
            _observation(100 + index, entity, "控评" if index == 0 else "普通")
            for index, entity in enumerate(entities[:2])
        ] + [
            _observation(200 + index, entity, "控评" if index < 5 else "普通")
            for index, entity in enumerate(entities[2:])
        ]
        session.add_all(entities + observations)
        session.add_all(
            [
                _hot_page(501, bvid, start + timedelta(minutes=10)),
                _hot_page(502, bvid, start + timedelta(hours=1, minutes=10)),
                _hot_observation(301, 3001, bvid, start, 501, 1),
                _hot_observation(302, 3002, bvid, start, 501, 2),
                _hot_observation(303, 3003, bvid, start, 502, 1),
                _hot_observation(304, 3004, bvid, start, 502, 2),
                VideoInfoSnapshot(
                    bvid=bvid,
                    captured_at=start + timedelta(hours=1, minutes=15),
                    title="Major creator response",
                    description=None,
                    owner_mid=999,
                    owner_name="Major UP",
                    tags={},
                    raw_payload_id=9001,
                ),
            ]
        )
        await session.commit()

        rows = await TurningPointAnalyzer(session).analyze(
            event_reference=event.id,
            since=start,
            until=start + timedelta(hours=2),
            bucket_seconds=3600,
            spike_multiplier=2.0,
            min_count=5,
            turnover_threshold=0.5,
            top_n=2,
        )

    by_type = {row.signal_type: row for row in rows}
    assert set(by_type) == {
        "comment_spike",
        "keyword_spike",
        "hot_turnover",
        "major_creator_involvement",
    }
    assert by_type["comment_spike"].evidence["previous_count"] == 2
    assert by_type["comment_spike"].evidence["current_count"] == 6
    assert by_type["keyword_spike"].evidence["keyword"] == "控评"
    assert by_type["keyword_spike"].evidence["current_count"] == 5
    assert by_type["hot_turnover"].evidence["turnover_rate"] == 1.0
    assert by_type["hot_turnover"].evidence["current_raw_page_id"] == 502
    assert by_type["major_creator_involvement"].evidence["owner_mid"] == 999
    assert by_type["major_creator_involvement"].evidence["raw_payload_id"] == 9001
    assert rows[0].as_dict()["interpretation_limit"] == (
        "heuristic_event_signal_not_causal_conclusion"
    )
    await engine.dispose()


def _entity(rpid: int, bvid: str, first_seen_at: datetime) -> CommentEntity:
    return CommentEntity(
        rpid=rpid,
        oid=777,
        bvid=bvid,
        root_rpid=None,
        parent_rpid=None,
        author_mid=rpid,
        author_name=f"user-{rpid}",
        first_content=f"comment-{rpid}",
        first_content_hash=bytes([rpid % 256]) * 32,
        first_seen_at=first_seen_at,
        first_raw_payload_id=rpid,
        created_at=first_seen_at,
        updated_at=first_seen_at,
    )


def _observation(
    observation_id: int,
    entity: CommentEntity,
    content: str,
) -> CommentObservation:
    return CommentObservation(
        id=observation_id,
        rpid=entity.rpid,
        bvid=entity.bvid,
        oid=entity.oid,
        captured_at=entity.first_seen_at,
        raw_payload_id=entity.first_raw_payload_id,
        raw_page_observation_id=None,
        sort_mode="latest",
        page_number=1,
        position=1,
        content=content,
        content_hash=bytes([observation_id % 256]) * 32,
        like_count=1,
        reply_count=0,
        author_mid=entity.author_mid,
        author_name=entity.author_name,
        is_deleted=False,
        visibility="visible",
        extra={},
    )


def _hot_page(page_id: int, bvid: str, captured_at: datetime) -> RawPageObservation:
    return RawPageObservation(
        id=page_id,
        raw_payload_id=page_id,
        captured_at=captured_at,
        request_type=BilibiliRequestType.COMMENT_HOT,
        target_type="video",
        target_id=bvid,
        page_number=1,
        cursor=None,
        sort_mode="hot",
        parser_version="test",
        status="success",
        item_count=2,
        extra={},
    )


def _hot_observation(
    observation_id: int,
    rpid: int,
    bvid: str,
    start: datetime,
    raw_page_id: int,
    position: int,
) -> CommentObservation:
    captured_at = start + (timedelta(hours=1) if raw_page_id == 502 else timedelta())
    return CommentObservation(
        id=observation_id,
        rpid=rpid,
        bvid=bvid,
        oid=777,
        captured_at=captured_at,
        raw_payload_id=raw_page_id,
        raw_page_observation_id=raw_page_id,
        sort_mode="hot",
        page_number=1,
        position=position,
        content="hot",
        content_hash=bytes([observation_id % 256]) * 32,
        like_count=1,
        reply_count=0,
        author_mid=rpid,
        author_name=f"user-{rpid}",
        is_deleted=False,
        visibility="visible",
        extra={},
    )
