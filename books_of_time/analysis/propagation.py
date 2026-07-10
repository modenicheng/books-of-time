from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import (
    CommentAnalysisFlag,
    CommentEntity,
    EventTarget,
    EventVideo,
)
from books_of_time.db.repositories import EventRepository


@dataclass(frozen=True, slots=True)
class PropagationNodeScore:
    event_id: int
    event_slug: str
    author_mid: int
    author_name: str | None
    role_scores: dict[str, float]
    overall_score: float
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "propagation-node-score-v1",
            "event_id": self.event_id,
            "event_slug": self.event_slug,
            "author_mid": self.author_mid,
            "author_name": self.author_name,
            "role_scores": self.role_scores,
            "overall_score": self.overall_score,
            "evidence": self.evidence,
            "algorithm": "event-comment-evidence-v1",
            "interpretation_limit": (
                "event_scoped_candidate_scores_not_identity_labels"
            ),
        }


class PropagationNodeAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        *,
        event_reference: int | str,
        since: datetime,
        until: datetime,
        max_comments: int = 50_000,
    ) -> list[PropagationNodeScore]:
        since_utc = _aware_utc(since, "since")
        until_utc = _aware_utc(until, "until")
        if until_utc <= since_utc:
            raise ValueError("until must be after since")
        if not 1 <= max_comments <= 500_000:
            raise ValueError("max_comments must be between 1 and 500000")
        event = await EventRepository(self.session).resolve_event(event_reference)
        bvids = list(
            await self.session.scalars(
                select(EventVideo.bvid).where(
                    EventVideo.event_id == event.id,
                    EventVideo.active.is_(True),
                )
            )
        )
        if not bvids:
            return []
        entities = list(
            await self.session.scalars(
                select(CommentEntity)
                .where(
                    CommentEntity.bvid.in_(bvids),
                    CommentEntity.first_seen_at >= since_utc,
                    CommentEntity.first_seen_at < until_utc,
                    CommentEntity.author_mid.is_not(None),
                )
                .order_by(CommentEntity.first_seen_at.asc(), CommentEntity.rpid.asc())
                .limit(max_comments + 1)
            )
        )
        if len(entities) > max_comments:
            raise ValueError(
                f"Propagation query exceeds max_comments={max_comments}; "
                "narrow the window"
            )
        if not entities:
            return []

        by_rpid = {entity.rpid: entity for entity in entities}
        evidence: dict[int, dict[str, Any]] = {}
        names: dict[int, str | None] = {}
        for entity in entities:
            mid = int(entity.author_mid)
            if mid not in names or names[mid] is None:
                names[mid] = entity.author_name
            row = evidence.setdefault(
                mid,
                {
                    "comment_count": 0,
                    "comment_rpids": set(),
                    "raw_payload_ids": set(),
                    "video_ids": set(),
                    "reply_comment_count": 0,
                    "template_origin_count": 0,
                    "template_amplifier_count": 0,
                    "template_flag_ids": set(),
                    "official_target_ids": [],
                },
            )
            row["comment_count"] += 1
            row["comment_rpids"].add(entity.rpid)
            if entity.first_raw_payload_id is not None:
                row["raw_payload_ids"].add(entity.first_raw_payload_id)
            row["video_ids"].add(entity.bvid)
            if entity.root_rpid not in (None, 0):
                row["reply_comment_count"] += 1

        flags = list(
            await self.session.scalars(
                select(CommentAnalysisFlag).where(
                    CommentAnalysisFlag.event_id == event.id,
                    CommentAnalysisFlag.flag_type == "template_like_comment",
                )
            )
        )
        for flag in flags:
            subject = by_rpid.get(flag.subject_rpid)
            related = by_rpid.get(flag.related_rpid) if flag.related_rpid else None
            if subject is not None and subject.author_mid is not None:
                evidence[int(subject.author_mid)]["template_origin_count"] += 1
                evidence[int(subject.author_mid)]["template_flag_ids"].add(flag.id)
            if related is not None and related.author_mid is not None:
                evidence[int(related.author_mid)]["template_amplifier_count"] += 1
                evidence[int(related.author_mid)]["template_flag_ids"].add(flag.id)

        targets = list(
            await self.session.scalars(
                select(EventTarget).where(
                    EventTarget.event_id == event.id,
                    EventTarget.target_type == "uid",
                    EventTarget.active.is_(True),
                )
            )
        )
        for target in targets:
            if str(target.extra.get("role", "")).casefold() != "official":
                continue
            mid = int(target.normalized_value)
            if mid in evidence:
                evidence[mid]["official_target_ids"].append(target.id)

        maxima = {
            "videos": max(len(row["video_ids"]) for row in evidence.values()),
            "replies": max(row["reply_comment_count"] for row in evidence.values()),
            "origins": max(row["template_origin_count"] for row in evidence.values()),
            "amplifiers": max(
                row["template_amplifier_count"] for row in evidence.values()
            ),
        }
        results: list[PropagationNodeScore] = []
        for mid, row in evidence.items():
            distinct_videos = len(row["video_ids"])
            scores = {
                "originator": _ratio(row["template_origin_count"], maxima["origins"]),
                "amplifier": _ratio(
                    row["template_amplifier_count"], maxima["amplifiers"]
                ),
                "bridge": (
                    (distinct_videos - 1) / (maxima["videos"] - 1)
                    if maxima["videos"] > 1
                    else 0.0
                ),
                "responder": _ratio(row["reply_comment_count"], maxima["replies"]),
                "official": 1.0 if row["official_target_ids"] else 0.0,
            }
            public_evidence = {
                "comment_count": row["comment_count"],
                "comment_rpids": sorted(row["comment_rpids"]),
                "raw_payload_ids": sorted(row["raw_payload_ids"]),
                "distinct_video_count": distinct_videos,
                "video_ids": sorted(row["video_ids"]),
                "reply_comment_count": row["reply_comment_count"],
                "template_origin_count": row["template_origin_count"],
                "template_amplifier_count": row["template_amplifier_count"],
                "template_flag_ids": sorted(row["template_flag_ids"]),
                "official_target_ids": sorted(row["official_target_ids"]),
                "window_start": since_utc.isoformat(),
                "window_end": until_utc.isoformat(),
            }
            results.append(
                PropagationNodeScore(
                    event_id=event.id,
                    event_slug=event.slug,
                    author_mid=mid,
                    author_name=names[mid],
                    role_scores={key: round(value, 6) for key, value in scores.items()},
                    overall_score=round(max(scores.values()), 6),
                    evidence=public_evidence,
                )
            )
        return sorted(results, key=lambda row: (-row.overall_score, row.author_mid))


def _ratio(value: int, maximum: int) -> float:
    return value / maximum if maximum else 0.0


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value.astimezone(UTC)
