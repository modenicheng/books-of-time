from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.analysis.hot_turnover import HotCommentTurnoverAnalyzer
from books_of_time.analysis.keywords import KeywordTrendAnalyzer
from books_of_time.analysis.templates import (
    TemplateCandidate,
    TemplateCandidateAnalyzer,
)
from books_of_time.analysis.turning_points import TurningPointAnalyzer
from books_of_time.db.models import (
    CommentObservation,
    EventKeyword,
    EventVideo,
    RawPageObservation,
    VideoInfoSnapshot,
    VideoMetricSnapshot,
)
from books_of_time.db.repositories import EventRepository
from books_of_time.domain.events import normalize_event_target


@dataclass(frozen=True, slots=True)
class EventReportOptions:
    bucket_seconds: int = 3600
    hot_top_n: int = 20
    spike_multiplier: float = 3.0
    spike_min_count: int = 5
    turnover_threshold: float = 0.5
    template_window_seconds: int = 3600
    template_min_similarity: float = 0.85
    template_min_text_chars: int = 8
    max_videos: int = 100
    max_records: int = 5000
    bvid: str | None = None
    keyword: str | None = None


@dataclass(frozen=True, slots=True)
class EventReport:
    generated_at: datetime
    window: dict[str, str]
    filters: dict[str, str | None]
    event: dict[str, Any]
    coverage: dict[str, Any]
    key_timeline: tuple[dict[str, Any], ...]
    core_videos: tuple[dict[str, Any], ...]
    hot_comment_changes: tuple[dict[str, Any], ...]
    keyword_trends: tuple[dict[str, Any], ...]
    template_clusters: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...]
    evidence_index: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "event-report-v1",
            "generated_at": self.generated_at.isoformat(),
            "window": self.window,
            "filters": self.filters,
            "event": self.event,
            "coverage": self.coverage,
            "key_timeline": list(self.key_timeline),
            "core_videos": list(self.core_videos),
            "hot_comment_changes": list(self.hot_comment_changes),
            "keyword_trends": list(self.keyword_trends),
            "template_clusters": list(self.template_clusters),
            "limitations": list(self.limitations),
            "evidence_index": list(self.evidence_index),
        }

    def render_markdown(self) -> str:
        sections = [
            f"# {self.event['name']}",
            "",
            "## 事件概述",
            _markdown_json(self.event),
            "",
            "## 筛选条件",
            _markdown_json(self.filters),
            "",
            "## 数据覆盖",
            _markdown_json(self.coverage),
            "",
            "## 关键时间线",
            _markdown_records(self.key_timeline),
            "",
            "## 核心视频节点",
            _markdown_records(self.core_videos),
            "",
            "## 热门评论变化",
            _markdown_records(self.hot_comment_changes),
            "",
            "## 关键词趋势",
            _markdown_records(self.keyword_trends),
            "",
            "## 模板化评论候选簇",
            _markdown_records(self.template_clusters),
            "",
            "## 结论限制",
            *(f"- {item}" for item in self.limitations),
            "",
            "## 证据索引",
            *(
                (f"- {row['kind']}:{row['id']}" for row in self.evidence_index)
                if self.evidence_index
                else ("- 无",)
            ),
            "",
        ]
        return "\n".join(sections)


class EventReportGenerator:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def generate(
        self,
        *,
        event_reference: int | str,
        since: datetime,
        until: datetime,
        options: EventReportOptions | None = None,
    ) -> EventReport:
        since_utc = _aware_utc(since, "since")
        until_utc = _aware_utc(until, "until")
        if until_utc <= since_utc:
            raise ValueError("until must be after since")
        selected = options or EventReportOptions()
        if not 1 <= selected.max_records <= 2_000_000:
            raise ValueError("max_records must be between 1 and 2000000")
        if not 1 <= selected.max_videos <= 1000:
            raise ValueError("max_videos must be between 1 and 1000")

        repository = EventRepository(self.session)
        event = await repository.resolve_event(event_reference)
        event_videos = list(
            await self.session.scalars(
                select(EventVideo)
                .where(EventVideo.event_id == event.id)
                .order_by(EventVideo.first_seen_at.asc(), EventVideo.bvid.asc())
            )
        )
        _check_limit("event videos", event_videos, selected.max_videos)
        active_bvids = [video.bvid for video in event_videos if video.active]
        selected_bvid = None
        if selected.bvid is not None:
            selected_bvid = normalize_event_target("seed_bvid", selected.bvid)
            if selected_bvid not in active_bvids:
                raise ValueError(
                    f"Video is not active in event {event.slug}: {selected_bvid}"
                )
            active_bvids = [selected_bvid]
            event_videos = [
                video for video in event_videos if video.bvid == selected_bvid
            ]
        selected_keyword = await self._resolve_keyword_filter(
            event.id,
            selected.keyword,
        )
        coverage = _coverage_dict(
            await repository.get_coverage_summary(
                event.id,
                since=since_utc,
                until=until_utc,
                bvids=tuple(active_bvids) if selected_bvid is not None else None,
            )
        )
        coverage["window"] = {
            "since": since_utc.isoformat(),
            "until": until_utc.isoformat(),
            "timestamp_field": "finished_at",
            "until_exclusive": True,
        }
        timeline_records = [
            row.as_dict()
            for row in await repository.build_timeline(
                event.id,
                since=since_utc,
                until=until_utc,
                max_records=selected.max_records,
                bvid=selected_bvid,
            )
        ]
        turning_points = await TurningPointAnalyzer(self.session).analyze(
            event_reference=event.id,
            since=since_utc,
            until=until_utc,
            bucket_seconds=selected.bucket_seconds,
            spike_multiplier=selected.spike_multiplier,
            min_count=selected.spike_min_count,
            turnover_threshold=selected.turnover_threshold,
            top_n=selected.hot_top_n,
            max_records=selected.max_records,
            bvid=selected_bvid,
            keyword=selected_keyword,
        )
        timeline_records.extend(point.as_dict() for point in turning_points)
        timeline_records.sort(key=_timeline_sort_key)
        _check_limit("timeline", timeline_records, selected.max_records)
        timeline = tuple(timeline_records)

        observations = await self._observations(
            active_bvids,
            since_utc,
            until_utc,
            selected.max_records,
            selected_keyword,
        )
        core_videos = tuple(await self._core_videos(event_videos, until_utc))
        hot_changes = tuple(
            await self._hot_changes(
                active_bvids,
                since_utc,
                until_utc,
                selected.hot_top_n,
                selected.max_records,
            )
        )
        keyword_trends = tuple(
            await self._keyword_trends(
                event.id,
                since_utc,
                until_utc,
                selected.bucket_seconds,
                observations,
                selected.max_records,
                selected_bvid,
                selected_keyword,
            )
        )
        template_clusters = tuple(
            await self._template_clusters(
                event.id,
                since_utc,
                until_utc,
                selected,
                selected_bvid,
                selected_keyword,
            )
        )
        report_sections: tuple[Any, ...] = (
            timeline,
            core_videos,
            hot_changes,
            keyword_trends,
            template_clusters,
        )
        evidence_index = tuple(_build_evidence_index(report_sections))
        limitations = tuple(_limitations(coverage, active_bvids))
        return EventReport(
            generated_at=datetime.now(UTC),
            window={"since": since_utc.isoformat(), "until": until_utc.isoformat()},
            filters={"bvid": selected_bvid, "keyword": selected_keyword},
            event={
                "id": event.id,
                "slug": event.slug,
                "name": event.name,
                "game": event.game,
                "description": event.description,
                "status": event.status,
                "start_at": _iso(event.start_at),
                "end_at": _iso(event.end_at),
                "timezone": event.timezone,
            },
            coverage=coverage,
            key_timeline=timeline,
            core_videos=core_videos,
            hot_comment_changes=hot_changes,
            keyword_trends=keyword_trends,
            template_clusters=template_clusters,
            limitations=limitations,
            evidence_index=evidence_index,
        )

    async def _resolve_keyword_filter(
        self,
        event_id: int,
        keyword: str | None,
    ) -> str | None:
        if keyword is None:
            return None
        normalized = normalize_event_target("keyword", keyword)
        exists = await self.session.scalar(
            select(EventKeyword.id).where(
                EventKeyword.event_id == event_id,
                EventKeyword.normalized_keyword == normalized,
                EventKeyword.active.is_(True),
            )
        )
        if exists is None:
            raise ValueError(f"Keyword is not active in event: {normalized}")
        return normalized

    async def _observations(
        self,
        bvids: list[str],
        since: datetime,
        until: datetime,
        max_records: int,
        keyword: str | None,
    ) -> list[CommentObservation]:
        if not bvids:
            return []
        stmt = select(CommentObservation).where(
            CommentObservation.bvid.in_(bvids),
            CommentObservation.captured_at >= since,
            CommentObservation.captured_at < until,
        )
        if keyword is not None:
            stmt = stmt.where(
                CommentObservation.content.is_not(None),
                func.lower(CommentObservation.content).contains(
                    keyword,
                    autoescape=True,
                ),
            )
        rows = list(
            await self.session.scalars(
                stmt.order_by(
                    CommentObservation.captured_at.asc(), CommentObservation.id
                ).limit(max_records + 1)
            )
        )
        _check_limit("comment observations", rows, max_records)
        return rows

    async def _core_videos(
        self,
        event_videos: list[EventVideo],
        until: datetime,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for video in event_videos:
            info = await self.session.scalar(
                select(VideoInfoSnapshot)
                .where(
                    VideoInfoSnapshot.bvid == video.bvid,
                    VideoInfoSnapshot.captured_at < until,
                )
                .order_by(VideoInfoSnapshot.captured_at.desc())
                .limit(1)
            )
            metric = await self.session.scalar(
                select(VideoMetricSnapshot)
                .where(
                    VideoMetricSnapshot.bvid == video.bvid,
                    VideoMetricSnapshot.captured_at < until,
                )
                .order_by(VideoMetricSnapshot.captured_at.desc())
                .limit(1)
            )
            records.append(
                {
                    "bvid": video.bvid,
                    "active": video.active,
                    "association_reason": video.association_reason,
                    "association_confidence": video.confidence,
                    "first_seen_at": video.first_seen_at.isoformat(),
                    "title": info.title if info else None,
                    "owner_mid": info.owner_mid if info else None,
                    "owner_name": info.owner_name if info else None,
                    "info_raw_payload_id": info.raw_payload_id if info else None,
                    "latest_metrics": (
                        {
                            "captured_at": metric.captured_at.isoformat(),
                            "view_count": metric.view_count,
                            "like_count": metric.like_count,
                            "coin_count": metric.coin_count,
                            "favorite_count": metric.favorite_count,
                            "share_count": metric.share_count,
                            "reply_count": metric.reply_count,
                            "danmaku_count": metric.danmaku_count,
                            "raw_payload_id": metric.raw_payload_id,
                        }
                        if metric
                        else None
                    ),
                }
            )
        return sorted(
            records,
            key=lambda record: (
                record["latest_metrics"] is None,
                record["title"] is None,
                -record["association_confidence"],
                record["first_seen_at"],
                record["bvid"],
            ),
        )

    async def _hot_changes(
        self,
        bvids: list[str],
        since: datetime,
        until: datetime,
        top_n: int,
        max_records: int,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        analyzer = HotCommentTurnoverAnalyzer(self.session)
        for bvid in bvids:
            records.extend(
                point.as_dict()
                for point in await analyzer.analyze(
                    bvid=bvid,
                    since=since,
                    until=until,
                    top_n=top_n,
                )
            )
            _check_limit("hot comment changes", records, max_records)
        page_ids = {
            record[key]
            for record in records
            for key in ("previous_raw_page_id", "current_raw_page_id")
        }
        pages: dict[int, RawPageObservation] = {}
        if page_ids:
            rows = await self.session.scalars(
                select(RawPageObservation).where(RawPageObservation.id.in_(page_ids))
            )
            pages = {row.id: row for row in rows}
        observations_by_page: dict[int, list[int]] = {}
        if page_ids:
            observations = await self.session.scalars(
                select(CommentObservation)
                .where(CommentObservation.raw_page_observation_id.in_(page_ids))
                .order_by(CommentObservation.id.asc())
            )
            for observation in observations:
                observations_by_page.setdefault(
                    observation.raw_page_observation_id, []
                ).append(observation.id)
        for record in records:
            for side in ("previous", "current"):
                page_id = record[f"{side}_raw_page_id"]
                page = pages.get(page_id)
                record[f"{side}_raw_payload_id"] = page.raw_payload_id if page else None
                record[f"{side}_comment_observation_ids"] = observations_by_page.get(
                    page_id, []
                )
        return records

    async def _keyword_trends(
        self,
        event_id: int,
        since: datetime,
        until: datetime,
        bucket_seconds: int,
        observations: list[CommentObservation],
        max_records: int,
        bvid: str | None,
        keyword: str | None,
    ) -> list[dict[str, Any]]:
        points = await KeywordTrendAnalyzer(self.session).analyze(
            event_reference=event_id,
            since=since,
            until=until,
            bucket_seconds=bucket_seconds,
            bvid=bvid,
            keyword=keyword,
        )
        _check_limit("keyword trends", points, max_records)
        records: list[dict[str, Any]] = []
        for point in points:
            matching = [
                row
                for row in observations
                if point.bucket_start <= row.captured_at < point.bucket_end
                and point.normalized_keyword in (row.content or "").casefold()
            ]
            record = point.as_dict()
            record["raw_payload_ids"] = sorted(
                {
                    row.raw_payload_id
                    for row in matching
                    if row.raw_payload_id is not None
                }
            )
            record["comment_observation_ids"] = sorted({row.id for row in matching})
            records.append(record)
        return records

    async def _template_clusters(
        self,
        event_id: int,
        since: datetime,
        until: datetime,
        options: EventReportOptions,
        bvid: str | None,
        keyword: str | None,
    ) -> list[dict[str, Any]]:
        candidates = await TemplateCandidateAnalyzer(self.session).analyze(
            event_reference=event_id,
            since=since,
            until=until,
            window_seconds=options.template_window_seconds,
            min_similarity=options.template_min_similarity,
            min_text_chars=options.template_min_text_chars,
            max_comments=max(2, min(options.max_records, 50_000)),
            bvid=bvid,
            keyword=keyword,
        )
        _check_limit("template candidates", candidates, options.max_records)
        return _cluster_candidates(candidates)


def _cluster_candidates(candidates: list[TemplateCandidate]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    parents = {
        rpid: rpid
        for candidate in candidates
        for rpid in (candidate.left_rpid, candidate.right_rpid)
    }

    def find(value: int) -> int:
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    for candidate in candidates:
        left_root = find(candidate.left_rpid)
        right_root = find(candidate.right_rpid)
        if left_root != right_root:
            parents[right_root] = left_root
    grouped: dict[int, list[TemplateCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(find(candidate.left_rpid), []).append(candidate)

    clusters: list[dict[str, Any]] = []
    for index, edges in enumerate(grouped.values(), start=1):
        members: dict[int, dict[str, Any]] = {}
        for edge in edges:
            members[edge.left_rpid] = _candidate_member(edge, "left")
            members[edge.right_rpid] = _candidate_member(edge, "right")
        ordered_members = [members[rpid] for rpid in sorted(members)]
        clusters.append(
            {
                "cluster_id": index,
                "candidate_only": True,
                "interpretation_limit": "candidate_only_not_proof_of_coordination",
                "member_rpids": sorted(members),
                "raw_payload_ids": sorted(
                    {
                        member["raw_payload_id"]
                        for member in ordered_members
                        if member["raw_payload_id"] is not None
                    }
                ),
                "members": ordered_members,
                "edges": [edge.as_dict() for edge in edges],
            }
        )
    return clusters


def _candidate_member(
    candidate: TemplateCandidate,
    side: str,
) -> dict[str, Any]:
    return {
        "rpid": getattr(candidate, f"{side}_rpid"),
        "bvid": getattr(candidate, f"{side}_bvid"),
        "author_mid": getattr(candidate, f"{side}_author_mid"),
        "author_name": getattr(candidate, f"{side}_author_name"),
        "content": getattr(candidate, f"{side}_content"),
        "first_seen_at": getattr(candidate, f"{side}_first_seen_at").isoformat(),
        "raw_payload_id": getattr(candidate, f"{side}_raw_payload_id"),
    }


def _coverage_dict(coverage: Any) -> dict[str, Any]:
    return {
        "active_video_count": coverage.active_video_count,
        "videos_with_coverage": coverage.videos_with_coverage,
        "video_coverage_ratio": coverage.video_coverage_ratio,
        "coverage_row_count": coverage.coverage_row_count,
        "succeeded_count": coverage.succeeded_count,
        "partial_count": coverage.partial_count,
        "failed_count": coverage.failed_count,
        "pages_requested": coverage.pages_requested,
        "pages_succeeded": coverage.pages_succeeded,
        "page_success_rate": coverage.page_success_rate,
        "items_observed": coverage.items_observed,
        "raw_payloads_saved": coverage.raw_payloads_saved,
        "parse_errors": coverage.parse_errors,
        "request_errors": coverage.request_errors,
        "truncated_count": coverage.truncated_count,
        "corrupted_count": coverage.corrupted_count,
        "first_started_at": _iso(coverage.first_started_at),
        "last_finished_at": _iso(coverage.last_finished_at),
    }


def _limitations(coverage: dict[str, Any], active_bvids: list[str]) -> list[str]:
    limitations = [
        "分析章节仅覆盖所选时间窗内成功采集到的公开数据。",
        "数据覆盖章节仅汇总 finished_at 落在所选半开时间窗内的采集记录。",
        "缺少观测不等同于平台删除，模板候选也不构成协同行为证明。",
        "所有启发式结论都需要结合证据索引和原始响应复核。",
    ]
    if not active_bvids:
        limitations.append("事件没有活动视频，因此分析章节为空。")
    elif coverage["video_coverage_ratio"] != 1:
        limitations.append("并非所有活动视频都有采集覆盖记录。")
    if coverage["partial_count"] or coverage["failed_count"]:
        limitations.append("事件存在部分成功或失败的采集任务。")
    if coverage["truncated_count"] or coverage["corrupted_count"]:
        limitations.append("事件存在截断或损坏的采集窗口。")
    return limitations


_EVIDENCE_KEYS = {
    "raw_payload": ("raw_payload_id", "raw_payload_ids"),
    "raw_page_observation": (
        "raw_page_id",
        "raw_page_ids",
        "raw_page_observation_id",
        "raw_page_observation_ids",
    ),
    "comment_observation": (
        "comment_observation_id",
        "comment_observation_ids",
        "observation_id",
        "observation_ids",
    ),
    "comment_analysis_flag": (
        "comment_analysis_flag_id",
        "comment_analysis_flag_ids",
        "flag_id",
        "flag_ids",
    ),
}


def _build_evidence_index(value: Any) -> list[dict[str, Any]]:
    found: set[tuple[str, int]] = set()

    def visit(current: Any, key: str | None = None) -> None:
        if isinstance(current, dict):
            for child_key, child in current.items():
                visit(child, child_key)
            return
        if isinstance(current, (list, tuple)):
            for child in current:
                visit(child, key)
            return
        if not isinstance(current, int) or key is None:
            return
        normalized = key.removeprefix("previous_").removeprefix("current_")
        normalized = normalized.removeprefix("info_")
        for kind, keys in _EVIDENCE_KEYS.items():
            if normalized in keys:
                found.add((kind, current))
                return

    visit(value)
    return [
        {"kind": kind, "id": identifier}
        for kind, identifier in sorted(found, key=lambda item: (item[0], item[1]))
    ]


def _check_limit(section: str, records: Any, max_records: int) -> None:
    if len(records) > max_records:
        raise ValueError(
            f"Report {section} exceeds max_records={max_records}; narrow the window"
        )


def _timeline_sort_key(record: dict[str, Any]) -> tuple[str, str]:
    timestamp = str(record.get("timestamp") or record.get("detected_at") or "")
    record_type = str(record.get("record_type") or record.get("signal_type") or "")
    return timestamp, record_type


def _markdown_json(value: dict[str, Any]) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


def _markdown_records(records: tuple[dict[str, Any], ...]) -> str:
    if not records:
        return "暂无数据。"
    return "\n\n".join(_markdown_json(record) for record in records)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value.astimezone(UTC)
