from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.propagation import PropagationNodeAnalyzer
from books_of_time.db.base import Base
from books_of_time.db.models import CommentAnalysisFlag, CommentEntity
from books_of_time.db.repositories import EventRepository


@pytest.mark.asyncio
async def test_propagation_node_scores_are_event_scoped_and_explainable() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(slug="event-a", name="事件 A", now=start)
        await repository.add_target(
            event_id=event.id,
            target_type="uid",
            target_value="1",
            extra={"role": "official"},
            now=start,
        )
        for bvid in ("BV1xx411c7mD", "BV1Q541167Qg"):
            await repository.attach_video(
                event_id=event.id,
                bvid=bvid,
                association_reason="manual",
                now=start,
            )
        entities = [
            _entity(101, "BV1xx411c7mD", 1, start, root_rpid=None),
            _entity(201, "BV1xx411c7mD", 2, start, root_rpid=None),
            _entity(202, "BV1Q541167Qg", 2, start, root_rpid=None),
            _entity(301, "BV1xx411c7mD", 3, start, root_rpid=101),
            _entity(401, "BV1xx411c7mD", 4, start, root_rpid=None),
            _entity(
                501, "BV1Q541167Qg", 5, start + timedelta(minutes=2), root_rpid=None
            ),
        ]
        session.add_all(entities)
        await session.flush()
        session.add(
            CommentAnalysisFlag(
                stable_key="a" * 64,
                event_id=event.id,
                flag_type="template_like_comment",
                subject_rpid=401,
                related_rpid=501,
                confidence=0.95,
                algorithm="sequence_matcher",
                algorithm_version="v1",
                evidence={},
                detected_at=start + timedelta(hours=2),
                created_at=start + timedelta(hours=2),
            )
        )
        await session.commit()

        rows = await PropagationNodeAnalyzer(session).analyze(
            event_reference="event-a",
            since=start,
            until=start + timedelta(hours=1),
        )

    by_mid = {row.author_mid: row for row in rows}
    assert by_mid[1].role_scores["official"] == 1.0
    assert by_mid[2].role_scores["bridge"] == 1.0
    assert by_mid[3].role_scores["responder"] == 1.0
    assert by_mid[4].role_scores["originator"] == 1.0
    assert by_mid[5].role_scores["amplifier"] == 1.0
    assert by_mid[2].evidence["distinct_video_count"] == 2
    assert by_mid[2].evidence["comment_rpids"] == [201, 202]
    assert by_mid[2].evidence["raw_payload_ids"] == [201, 202]
    assert by_mid[4].evidence["template_origin_count"] == 1
    assert by_mid[4].evidence["template_flag_ids"] == [1]
    assert by_mid[5].evidence["template_amplifier_count"] == 1
    assert by_mid[1].as_dict()["interpretation_limit"] == (
        "event_scoped_candidate_scores_not_identity_labels"
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_propagation_nodes_enforce_comment_limit() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(slug="event-a", name="A", now=start)
        await repository.attach_video(
            event_id=event.id,
            bvid="BV1xx411c7mD",
            association_reason="manual",
            now=start,
        )
        session.add_all(
            [
                _entity(101, "BV1xx411c7mD", 1, start, root_rpid=None),
                _entity(102, "BV1xx411c7mD", 2, start, root_rpid=None),
            ]
        )
        await session.commit()

        with pytest.raises(ValueError, match="max_comments"):
            await PropagationNodeAnalyzer(session).analyze(
                event_reference=event.id,
                since=start,
                until=start + timedelta(hours=1),
                max_comments=1,
            )
    await engine.dispose()


def _entity(
    rpid: int,
    bvid: str,
    author_mid: int,
    first_seen_at: datetime,
    *,
    root_rpid: int | None,
) -> CommentEntity:
    return CommentEntity(
        rpid=rpid,
        oid=777,
        bvid=bvid,
        root_rpid=root_rpid,
        parent_rpid=root_rpid,
        author_mid=author_mid,
        author_name=f"user-{author_mid}",
        first_content=f"comment-{rpid}",
        first_content_hash=bytes([rpid % 256]) * 32,
        first_seen_at=first_seen_at,
        first_raw_payload_id=rpid,
        created_at=first_seen_at,
        updated_at=first_seen_at,
    )
