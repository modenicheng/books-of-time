from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.replay import EventPropagationReplayAnalyzer
from books_of_time.db.base import Base
from books_of_time.db.models import CommentAnalysisFlag, CommentEntity
from books_of_time.db.repositories import EventRepository


@pytest.mark.asyncio
async def test_event_propagation_replay_orders_only_evidenced_directed_edges() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    async with factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(slug="event-a", name="A", now=start)
        await repository.attach_video(
            event_id=event.id,
            bvid="BV1xx411c7mD",
            association_reason="seed_bvid",
            now=start + timedelta(minutes=1),
        )
        await repository.attach_video(
            event_id=event.id,
            bvid="BV1Q541167Qg",
            association_reason="uid_discovery",
            now=start + timedelta(minutes=2),
        )
        root = _entity(101, "BV1xx411c7mD", 1, start + timedelta(minutes=3))
        reply = _entity(
            102,
            "BV1xx411c7mD",
            2,
            start + timedelta(minutes=4),
            root_rpid=101,
        )
        copied = _entity(201, "BV1Q541167Qg", 3, start + timedelta(minutes=5))
        session.add_all([root, reply, copied])
        await session.flush()
        session.add(
            CommentAnalysisFlag(
                stable_key="f" * 64,
                event_id=event.id,
                flag_type="template_like_comment",
                subject_rpid=101,
                related_rpid=201,
                confidence=0.96,
                algorithm="sequence_matcher",
                algorithm_version="v1",
                evidence={"candidate_reason": "similar_text_cross_video"},
                detected_at=start + timedelta(hours=1),
                created_at=start + timedelta(hours=1),
            )
        )
        await session.commit()

        rows = await EventPropagationReplayAnalyzer(session).analyze(
            event_reference=event.id,
            since=start,
            until=start + timedelta(hours=1),
        )

    assert [row.record_type for row in rows] == [
        "video_associated",
        "video_associated",
        "comment_reply",
        "template_propagation",
    ]
    reply_row = rows[2]
    assert reply_row.source["rpid"] == 101
    assert reply_row.target["rpid"] == 102
    assert reply_row.target["author_mid"] == 2
    assert reply_row.evidence["target_raw_payload_id"] == 102
    template_row = rows[3]
    assert template_row.source["bvid"] == "BV1xx411c7mD"
    assert template_row.target["bvid"] == "BV1Q541167Qg"
    assert template_row.evidence["comment_analysis_flag_id"] == 1
    assert template_row.as_dict()["interpretation_limit"] == (
        "evidenced_edges_only_not_complete_causal_graph"
    )
    await engine.dispose()


def _entity(
    rpid: int,
    bvid: str,
    author_mid: int,
    first_seen_at: datetime,
    *,
    root_rpid: int | None = None,
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
