from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.analysis.report import EventReportGenerator, EventReportOptions
from books_of_time.db.base import Base
from books_of_time.db.models import (
    CollectionCoverageStat,
    CommentEntity,
    CommentObservation,
    RawPageObservation,
    VideoInfoSnapshot,
    VideoMetricSnapshot,
)
from books_of_time.db.repositories import EventRepository
from books_of_time.domain.enums import BilibiliRequestType, TaskKind


@pytest.mark.asyncio
async def test_event_report_renders_all_sections_and_honest_empty_limits() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, tzinfo=UTC)
    async with factory() as session:
        await EventRepository(session).create_event(
            slug="empty-event",
            name="空事件",
            description="尚无采集结果",
            now=start,
        )
        await session.commit()
        report = await EventReportGenerator(session).generate(
            event_reference="empty-event",
            since=start,
            until=start + timedelta(hours=2),
        )
        with pytest.raises(ValueError, match="not active"):
            await EventReportGenerator(session).generate(
                event_reference="empty-event",
                since=start,
                until=start + timedelta(hours=2),
                options=EventReportOptions(bvid="BV1xx411c7mD"),
            )
        with pytest.raises(ValueError, match="Keyword is not active"):
            await EventReportGenerator(session).generate(
                event_reference="empty-event",
                since=start,
                until=start + timedelta(hours=2),
                options=EventReportOptions(keyword="不存在"),
            )

    assert report.event["name"] == "空事件"
    assert report.coverage["active_video_count"] == 0
    assert report.core_videos == ()
    assert report.evidence_index == ()
    assert any("没有活动视频" in item for item in report.limitations)
    markdown = report.render_markdown()
    for heading in (
        "# 空事件",
        "## 事件概述",
        "## 数据覆盖",
        "## 关键时间线",
        "## 核心视频节点",
        "## 热门评论变化",
        "## 关键词趋势",
        "## 模板化评论候选簇",
        "## 结论限制",
        "## 证据索引",
    ):
        assert heading in markdown
    assert report.as_dict()["schema_version"] == "event-report-v1"
    await engine.dispose()


@pytest.mark.asyncio
async def test_event_report_preserves_raw_evidence_across_summary_sections() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    start = datetime(2026, 7, 10, 10, tzinfo=UTC)
    left_bvid = "BV1xx411c7mD"
    right_bvid = "BV1Q541167Qg"
    async with factory() as session:
        repository = EventRepository(session)
        event = await repository.create_event(
            slug="event-a",
            name="事件 A",
            game="Example Game",
            description="用于报告验收",
            start_at=start,
            now=start,
        )
        await repository.add_target(
            event_id=event.id,
            target_type="keyword",
            target_value="控评",
            now=start,
        )
        await repository.add_target(
            event_id=event.id,
            target_type="keyword",
            target_value="删评",
            now=start,
        )
        for bvid in (left_bvid, right_bvid):
            await repository.attach_video(
                event_id=event.id,
                bvid=bvid,
                association_reason="manual",
                now=start + timedelta(minutes=1),
            )
        session.add_all(
            [
                _failed_coverage(right_bvid, start - timedelta(days=1)),
                _coverage(left_bvid, start),
                VideoInfoSnapshot(
                    bvid=left_bvid,
                    captured_at=start + timedelta(minutes=10),
                    title="核心视频",
                    description=None,
                    owner_mid=900,
                    owner_name="公开 UP",
                    tags={},
                    raw_payload_id=801,
                ),
                VideoMetricSnapshot(
                    bvid=left_bvid,
                    captured_at=start + timedelta(minutes=20),
                    view_count=1000,
                    like_count=100,
                    coin_count=10,
                    favorite_count=20,
                    share_count=30,
                    reply_count=40,
                    danmaku_count=50,
                    raw_payload_id=701,
                ),
                _entity(
                    1001,
                    left_bvid,
                    11,
                    "甲",
                    start + timedelta(minutes=15),
                    "统一回复: 控评说明",
                ),
                _entity(
                    1002,
                    right_bvid,
                    22,
                    "乙",
                    start + timedelta(minutes=16),
                    "统一回复, 控评说明!",
                ),
                _observation(
                    101,
                    1001,
                    left_bvid,
                    start + timedelta(minutes=15),
                    "统一回复: 控评说明",
                    1001,
                ),
                _observation(
                    102,
                    1002,
                    right_bvid,
                    start + timedelta(minutes=16),
                    "统一回复, 控评说明!",
                    1002,
                ),
                _hot_page(501, left_bvid, start + timedelta(minutes=30), 901),
                _hot_page(502, left_bvid, start + timedelta(minutes=60), 902),
                _hot_observation(
                    201,
                    2001,
                    left_bvid,
                    start + timedelta(minutes=30),
                    501,
                ),
                _hot_observation(
                    202,
                    2002,
                    left_bvid,
                    start + timedelta(minutes=60),
                    502,
                ),
            ]
        )
        await session.commit()

        report = await EventReportGenerator(session).generate(
            event_reference=event.id,
            since=start,
            until=start + timedelta(hours=2),
            options=EventReportOptions(
                bucket_seconds=3600,
                template_min_similarity=0.9,
                template_min_text_chars=8,
                hot_top_n=1,
            ),
        )
        filtered = await EventReportGenerator(session).generate(
            event_reference=event.id,
            since=start,
            until=start + timedelta(hours=2),
            options=EventReportOptions(
                bucket_seconds=3600,
                template_min_similarity=0.9,
                template_min_text_chars=8,
                hot_top_n=1,
                bvid=left_bvid,
                keyword="控评",
            ),
        )

    assert report.coverage["coverage_row_count"] == 1
    assert report.coverage["video_coverage_ratio"] == 0.5
    assert report.coverage["failed_count"] == 0
    assert not any("当前全部采集记录" in item for item in report.limitations)
    assert report.core_videos[0]["title"] == "核心视频"
    assert report.core_videos[0]["info_raw_payload_id"] == 801
    assert report.hot_comment_changes[0]["current_raw_payload_id"] == 902
    control_trend = next(
        row for row in report.keyword_trends if row["keyword"] == "控评"
    )
    assert control_trend["raw_payload_ids"] == [1001, 1002]
    assert report.template_clusters[0]["member_rpids"] == [1001, 1002]
    assert report.template_clusters[0]["raw_payload_ids"] == [1001, 1002]
    evidence = {(row["kind"], row["id"]) for row in report.evidence_index}
    assert ("raw_payload", 801) in evidence
    assert ("raw_payload", 902) in evidence
    assert ("raw_page_observation", 501) in evidence
    assert ("comment_observation", 101) in evidence
    markdown = report.render_markdown()
    assert "核心视频" in markdown
    assert "raw_payload:801" in markdown
    assert "candidate_only_not_proof_of_coordination" in markdown
    assert filtered.filters == {"bvid": left_bvid, "keyword": "控评"}
    assert filtered.coverage["active_video_count"] == 1
    assert filtered.coverage["video_coverage_ratio"] == 1
    assert [video["bvid"] for video in filtered.core_videos] == [left_bvid]
    assert {row["keyword"] for row in filtered.keyword_trends} == {"控评"}
    assert filtered.template_clusters == ()
    assert all(row.get("bvid", left_bvid) == left_bvid for row in filtered.key_timeline)
    assert filtered.as_dict()["filters"] == {
        "bvid": left_bvid,
        "keyword": "控评",
    }
    await engine.dispose()


def _coverage(bvid: str, started_at: datetime) -> CollectionCoverageStat:
    return CollectionCoverageStat(
        collection_task_id=1,
        run_id="report-run",
        task_kind=TaskKind.FETCH_HOT_COMMENTS,
        target_type="video",
        target_id=bvid,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=5),
        status="succeeded",
        pages_requested=2,
        pages_succeeded=2,
        items_observed=2,
        raw_payloads_saved=2,
        parse_errors=0,
        request_errors=0,
        frontier_reached=True,
        frontier_missing=False,
        truncated=False,
        corrupted=False,
        reason=None,
        extra={},
        created_at=started_at,
        updated_at=started_at,
    )


def _failed_coverage(bvid: str, started_at: datetime) -> CollectionCoverageStat:
    row = _coverage(bvid, started_at)
    row.collection_task_id = 99
    row.run_id = "old-report-run"
    row.status = "failed"
    row.pages_succeeded = 0
    row.request_errors = 2
    row.frontier_reached = False
    return row


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
        first_content_hash=bytes([rpid % 256]) * 32,
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
    content: str,
    raw_payload_id: int,
) -> CommentObservation:
    return CommentObservation(
        id=observation_id,
        rpid=rpid,
        bvid=bvid,
        oid=777,
        captured_at=captured_at,
        raw_payload_id=raw_payload_id,
        raw_page_observation_id=None,
        sort_mode="latest",
        page_number=1,
        position=1,
        content=content,
        content_hash=bytes([observation_id]) * 32,
        like_count=1,
        reply_count=0,
        author_mid=rpid,
        author_name=f"user-{rpid}",
        is_deleted=False,
        visibility="visible",
        extra={},
    )


def _hot_page(
    page_id: int,
    bvid: str,
    captured_at: datetime,
    raw_payload_id: int,
) -> RawPageObservation:
    return RawPageObservation(
        id=page_id,
        raw_payload_id=raw_payload_id,
        captured_at=captured_at,
        request_type=BilibiliRequestType.COMMENT_HOT,
        target_type="video",
        target_id=bvid,
        page_number=1,
        cursor=None,
        sort_mode="hot",
        parser_version="test",
        status="success",
        item_count=1,
        extra={},
    )


def _hot_observation(
    observation_id: int,
    rpid: int,
    bvid: str,
    captured_at: datetime,
    raw_page_id: int,
) -> CommentObservation:
    return CommentObservation(
        id=observation_id,
        rpid=rpid,
        bvid=bvid,
        oid=777,
        captured_at=captured_at,
        raw_payload_id=900 + (raw_page_id - 500),
        raw_page_observation_id=raw_page_id,
        sort_mode="hot",
        page_number=1,
        position=1,
        content="热门评论",
        content_hash=bytes([observation_id]) * 32,
        like_count=10,
        reply_count=1,
        author_mid=rpid,
        author_name=f"user-{rpid}",
        is_deleted=False,
        visibility="visible",
        extra={},
    )
