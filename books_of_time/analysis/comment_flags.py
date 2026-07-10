from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.analysis.templates import TemplateCandidateAnalyzer
from books_of_time.db.models import (
    CommentAnalysisFlag,
    CommentEntity,
    CommentObservation,
    EventVideo,
)
from books_of_time.db.repositories import EventRepository


@dataclass(frozen=True, slots=True)
class CommentFlagRefreshSummary:
    event_id: int
    event_slug: str
    since: datetime
    until: datetime
    detected_at: datetime
    matched_count: int
    created_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "comment-flag-refresh-v1",
            "event_id": self.event_id,
            "event_slug": self.event_slug,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "detected_at": self.detected_at.isoformat(),
            "matched_count": self.matched_count,
            "created_count": self.created_count,
        }


@dataclass(frozen=True, slots=True)
class _FlagDraft:
    flag_type: str
    subject_rpid: int
    related_rpid: int | None
    confidence: float
    algorithm: str
    algorithm_version: str
    evidence: dict[str, Any]


class CommentFlagAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def refresh(
        self,
        *,
        event_reference: int | str,
        since: datetime,
        until: datetime,
        detected_at: datetime,
        template_window_seconds: int = 3600,
        template_min_similarity: float = 0.85,
        template_min_text_chars: int = 8,
        max_comments: int = 5000,
        max_comparisons: int = 100_000,
    ) -> CommentFlagRefreshSummary:
        since_utc = _aware_utc(since, "since")
        until_utc = _aware_utc(until, "until")
        detected_utc = _aware_utc(detected_at, "detected_at")
        if until_utc <= since_utc:
            raise ValueError("until must be after since")

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
            return CommentFlagRefreshSummary(
                event_id=event.id,
                event_slug=event.slug,
                since=since_utc,
                until=until_utc,
                detected_at=detected_utc,
                matched_count=0,
                created_count=0,
            )

        drafts: list[_FlagDraft] = []
        drafts.extend(
            await self._duplicate_display_drafts(
                bvids=bvids,
                since=since_utc,
                until=until_utc,
            )
        )
        drafts.extend(
            await self._same_user_submission_drafts(
                bvids=bvids,
                since=since_utc,
                until=until_utc,
            )
        )
        template_version = (
            f"sequence_matcher-v1:min={template_min_similarity:.6f}:"
            f"window={template_window_seconds}:minchars={template_min_text_chars}"
        )
        candidates = await TemplateCandidateAnalyzer(self.session).analyze(
            event_reference=event.id,
            since=since_utc,
            until=until_utc,
            window_seconds=template_window_seconds,
            min_similarity=template_min_similarity,
            min_text_chars=template_min_text_chars,
            max_comments=max_comments,
            max_comparisons=max_comparisons,
        )
        for candidate in candidates:
            drafts.append(
                _FlagDraft(
                    flag_type="template_like_comment",
                    subject_rpid=candidate.left_rpid,
                    related_rpid=candidate.right_rpid,
                    confidence=candidate.similarity,
                    algorithm="sequence_matcher",
                    algorithm_version=template_version,
                    evidence=candidate.as_dict(),
                )
            )

        created_count = 0
        for draft in drafts:
            stable_key = _stable_key(event.id, draft)
            existing = await self.session.scalar(
                select(CommentAnalysisFlag.id).where(
                    CommentAnalysisFlag.stable_key == stable_key
                )
            )
            if existing is not None:
                continue
            self.session.add(
                CommentAnalysisFlag(
                    stable_key=stable_key,
                    event_id=event.id,
                    flag_type=draft.flag_type,
                    subject_rpid=draft.subject_rpid,
                    related_rpid=draft.related_rpid,
                    confidence=draft.confidence,
                    algorithm=draft.algorithm,
                    algorithm_version=draft.algorithm_version,
                    evidence=draft.evidence,
                    detected_at=detected_utc,
                    created_at=detected_utc,
                )
            )
            created_count += 1
        await self.session.flush()
        return CommentFlagRefreshSummary(
            event_id=event.id,
            event_slug=event.slug,
            since=since_utc,
            until=until_utc,
            detected_at=detected_utc,
            matched_count=len(drafts),
            created_count=created_count,
        )

    async def _duplicate_display_drafts(
        self,
        *,
        bvids: list[str],
        since: datetime,
        until: datetime,
    ) -> list[_FlagDraft]:
        observations = list(
            await self.session.scalars(
                select(CommentObservation)
                .where(
                    CommentObservation.bvid.in_(bvids),
                    CommentObservation.captured_at >= since,
                    CommentObservation.captured_at < until,
                    CommentObservation.raw_page_observation_id.is_not(None),
                )
                .order_by(CommentObservation.id.asc())
            )
        )
        grouped: dict[tuple[int, int], list[CommentObservation]] = {}
        for observation in observations:
            key = (int(observation.raw_page_observation_id), observation.rpid)
            grouped.setdefault(key, []).append(observation)
        drafts: list[_FlagDraft] = []
        for (raw_page_id, rpid), rows in grouped.items():
            if len(rows) < 2:
                continue
            drafts.append(
                _FlagDraft(
                    flag_type="same_rpid_duplicate_display",
                    subject_rpid=rpid,
                    related_rpid=None,
                    confidence=1.0,
                    algorithm="raw_page_duplicate",
                    algorithm_version="raw-page-duplicate-v1",
                    evidence={
                        "raw_page_observation_id": raw_page_id,
                        "observation_ids": [row.id for row in rows],
                        "display_count": len(rows),
                    },
                )
            )
        return drafts

    async def _same_user_submission_drafts(
        self,
        *,
        bvids: list[str],
        since: datetime,
        until: datetime,
    ) -> list[_FlagDraft]:
        entities = list(
            await self.session.scalars(
                select(CommentEntity)
                .where(
                    CommentEntity.bvid.in_(bvids),
                    CommentEntity.first_seen_at >= since,
                    CommentEntity.first_seen_at < until,
                    CommentEntity.author_mid.is_not(None),
                    CommentEntity.first_content.is_not(None),
                )
                .order_by(CommentEntity.rpid.asc())
            )
        )
        grouped: dict[tuple[int, str], list[CommentEntity]] = {}
        for entity in entities:
            normalized = _normalize_text(entity.first_content or "")
            if not normalized:
                continue
            grouped.setdefault((int(entity.author_mid), normalized), []).append(entity)
        drafts: list[_FlagDraft] = []
        for (author_mid, normalized), rows in grouped.items():
            for index, left in enumerate(rows):
                for right in rows[index + 1 :]:
                    drafts.append(
                        _FlagDraft(
                            flag_type="same_user_duplicate_submission",
                            subject_rpid=left.rpid,
                            related_rpid=right.rpid,
                            confidence=1.0,
                            algorithm="normalized_exact",
                            algorithm_version="normalized-exact-v1",
                            evidence={
                                "author_mid": author_mid,
                                "subject_bvid": left.bvid,
                                "related_bvid": right.bvid,
                                "subject_raw_payload_id": left.first_raw_payload_id,
                                "related_raw_payload_id": right.first_raw_payload_id,
                                "normalized_content_sha256": hashlib.sha256(
                                    normalized.encode()
                                ).hexdigest(),
                            },
                        )
                    )
        return drafts


def _stable_key(event_id: int, draft: _FlagDraft) -> str:
    identity = {
        "event_id": event_id,
        "flag_type": draft.flag_type,
        "subject_rpid": draft.subject_rpid,
        "related_rpid": draft.related_rpid,
        "algorithm_version": draft.algorithm_version,
        "evidence_scope": (
            draft.evidence.get("raw_page_observation_id")
            if draft.flag_type == "same_rpid_duplicate_display"
            else None
        ),
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value.astimezone(UTC)
