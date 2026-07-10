from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import CommentEntity, EventVideo
from books_of_time.db.repositories import EventRepository


@dataclass(frozen=True, slots=True)
class TemplateCandidate:
    event_id: int
    event_slug: str
    left_rpid: int
    left_bvid: str
    left_author_mid: int | None
    left_author_name: str | None
    left_content: str
    left_first_seen_at: datetime
    left_raw_payload_id: int | None
    right_rpid: int
    right_bvid: str
    right_author_mid: int | None
    right_author_name: str | None
    right_content: str
    right_first_seen_at: datetime
    right_raw_payload_id: int | None
    similarity: float
    time_delta_seconds: int
    candidate_reason: str
    min_similarity: float
    window_seconds: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "template-candidate-v1",
            "event_id": self.event_id,
            "event_slug": self.event_slug,
            "left_rpid": self.left_rpid,
            "left_bvid": self.left_bvid,
            "left_author_mid": self.left_author_mid,
            "left_author_name": self.left_author_name,
            "left_content": self.left_content,
            "left_first_seen_at": self.left_first_seen_at.isoformat(),
            "left_raw_payload_id": self.left_raw_payload_id,
            "right_rpid": self.right_rpid,
            "right_bvid": self.right_bvid,
            "right_author_mid": self.right_author_mid,
            "right_author_name": self.right_author_name,
            "right_content": self.right_content,
            "right_first_seen_at": self.right_first_seen_at.isoformat(),
            "right_raw_payload_id": self.right_raw_payload_id,
            "similarity": self.similarity,
            "time_delta_seconds": self.time_delta_seconds,
            "candidate_reason": self.candidate_reason,
            "algorithm": "sequence_matcher-v1",
            "min_similarity": self.min_similarity,
            "window_seconds": self.window_seconds,
            "interpretation_limit": "candidate_only_not_proof_of_coordination",
        }


class TemplateCandidateAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        *,
        event_reference: int | str,
        since: datetime,
        until: datetime,
        window_seconds: int = 3600,
        min_similarity: float = 0.85,
        min_text_chars: int = 8,
        max_comments: int = 5000,
        max_comparisons: int = 100_000,
    ) -> list[TemplateCandidate]:
        since_utc = _aware_utc(since, "since")
        until_utc = _aware_utc(until, "until")
        if until_utc <= since_utc:
            raise ValueError("until must be after since")
        if not 60 <= window_seconds <= 86_400:
            raise ValueError("window_seconds must be between 60 and 86400")
        if not 0.5 <= min_similarity <= 1:
            raise ValueError("min_similarity must be between 0.5 and 1")
        if not 4 <= min_text_chars <= 1000:
            raise ValueError("min_text_chars must be between 4 and 1000")
        if not 2 <= max_comments <= 50_000:
            raise ValueError("max_comments must be between 2 and 50000")
        if not 1 <= max_comparisons <= 5_000_000:
            raise ValueError("max_comparisons must be between 1 and 5000000")

        event = await EventRepository(self.session).resolve_event(event_reference)
        bvids = list(
            await self.session.scalars(
                select(EventVideo.bvid)
                .where(
                    EventVideo.event_id == event.id,
                    EventVideo.active.is_(True),
                )
                .order_by(EventVideo.bvid.asc())
            )
        )
        if not bvids:
            return []

        comments = list(
            await self.session.scalars(
                select(CommentEntity)
                .where(
                    CommentEntity.bvid.in_(bvids),
                    CommentEntity.first_seen_at >= since_utc,
                    CommentEntity.first_seen_at < until_utc,
                    CommentEntity.first_content.is_not(None),
                )
                .order_by(CommentEntity.first_seen_at.asc(), CommentEntity.rpid.asc())
                .limit(max_comments + 1)
            )
        )
        if len(comments) > max_comments:
            raise ValueError(
                f"Template query exceeds max_comments={max_comments}; narrow the window"
            )

        normalized = {
            comment.rpid: _normalize_template_text(comment.first_content or "")
            for comment in comments
        }
        candidates: list[TemplateCandidate] = []
        comparisons = 0
        for left_index, left in enumerate(comments):
            left_text = normalized[left.rpid]
            if len(left_text) < min_text_chars:
                continue
            for right in comments[left_index + 1 :]:
                delta_seconds = int(
                    (right.first_seen_at - left.first_seen_at).total_seconds()
                )
                if delta_seconds > window_seconds:
                    break
                if left.bvid == right.bvid:
                    continue
                right_text = normalized[right.rpid]
                if len(right_text) < min_text_chars:
                    continue
                if _length_ratio_upper_bound(left_text, right_text) < min_similarity:
                    continue
                comparisons += 1
                if comparisons > max_comparisons:
                    raise ValueError(
                        "Template query exceeds max_comparisons; narrow the window "
                        "or raise the configured limit"
                    )
                similarity = SequenceMatcher(
                    None,
                    left_text,
                    right_text,
                    autojunk=False,
                ).ratio()
                if similarity < min_similarity:
                    continue
                candidates.append(
                    _candidate(
                        event_id=event.id,
                        event_slug=event.slug,
                        left=left,
                        right=right,
                        similarity=similarity,
                        time_delta_seconds=delta_seconds,
                        min_similarity=min_similarity,
                        window_seconds=window_seconds,
                        normalized_exact=left_text == right_text,
                    )
                )
        return candidates


def _candidate(
    *,
    event_id: int,
    event_slug: str,
    left: CommentEntity,
    right: CommentEntity,
    similarity: float,
    time_delta_seconds: int,
    min_similarity: float,
    window_seconds: int,
    normalized_exact: bool,
) -> TemplateCandidate:
    return TemplateCandidate(
        event_id=event_id,
        event_slug=event_slug,
        left_rpid=left.rpid,
        left_bvid=left.bvid,
        left_author_mid=left.author_mid,
        left_author_name=left.author_name,
        left_content=left.first_content or "",
        left_first_seen_at=left.first_seen_at,
        left_raw_payload_id=left.first_raw_payload_id,
        right_rpid=right.rpid,
        right_bvid=right.bvid,
        right_author_mid=right.author_mid,
        right_author_name=right.author_name,
        right_content=right.first_content or "",
        right_first_seen_at=right.first_seen_at,
        right_raw_payload_id=right.first_raw_payload_id,
        similarity=round(similarity, 6),
        time_delta_seconds=time_delta_seconds,
        candidate_reason=(
            "normalized_exact_text_cross_video"
            if normalized_exact
            else "similar_text_cross_video"
        ),
        min_similarity=min_similarity,
        window_seconds=window_seconds,
    )


def _normalize_template_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _length_ratio_upper_bound(left: str, right: str) -> float:
    return 2 * min(len(left), len(right)) / (len(left) + len(right))


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value.astimezone(UTC)
