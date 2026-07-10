from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import CommentObservation, EventVideo
from books_of_time.db.repositories import EventRepository

STANCE_CATEGORIES: Final[tuple[str, ...]] = ("support", "criticism", "neutral")


@dataclass(frozen=True, slots=True)
class StanceLexicon:
    version: str
    terms: Mapping[str, tuple[str, ...]]

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> StanceLexicon:
        if not isinstance(config, Mapping):
            raise ValueError("stance_lexicon must be a mapping")
        version = config.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("stance_lexicon.version must be a non-empty string")

        unsupported = set(config) - {"version", *STANCE_CATEGORIES}
        if unsupported:
            names = ", ".join(sorted(str(name) for name in unsupported))
            raise ValueError(f"Unsupported stance_lexicon keys: {names}")

        terms: dict[str, tuple[str, ...]] = {}
        owners: dict[str, str] = {}
        for category in STANCE_CATEGORIES:
            configured = config.get(category, [])
            if isinstance(configured, str) or not isinstance(configured, Sequence):
                raise ValueError(f"stance_lexicon.{category} must be a list")
            normalized_terms: list[str] = []
            for value in configured:
                if not isinstance(value, str):
                    raise ValueError(f"stance_lexicon.{category} terms must be strings")
                term = _normalize_text(value)
                if not term:
                    raise ValueError(
                        f"stance_lexicon.{category} terms must not be empty"
                    )
                owner = owners.get(term)
                if owner is not None and owner != category:
                    raise ValueError(
                        f"Term {term!r} appears in multiple categories: "
                        f"{owner}, {category}"
                    )
                owners[term] = category
                if term not in normalized_terms:
                    normalized_terms.append(term)
            terms[category] = tuple(normalized_terms)
        return cls(version=version.strip(), terms=terms)


@dataclass(frozen=True, slots=True)
class StanceEvidenceSummary:
    event_id: int
    event_slug: str
    scope_type: str
    scope_id: str
    since: datetime
    until: datetime
    lexicon_version: str
    category: str
    lexicon_terms: tuple[str, ...]
    distinct_comment_count: int
    observation_count: int
    matched_term_counts: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "stance-evidence-v1",
            "event_id": self.event_id,
            "event_slug": self.event_slug,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "lexicon_version": self.lexicon_version,
            "category": self.category,
            "lexicon_terms": list(self.lexicon_terms),
            "distinct_comment_count": self.distinct_comment_count,
            "observation_count": self.observation_count,
            "matched_term_counts": dict(self.matched_term_counts),
        }


class StanceEvidenceAnalyzer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        *,
        event_reference: int | str,
        since: datetime,
        until: datetime,
        lexicon: StanceLexicon,
        bvid: str | None = None,
    ) -> list[StanceEvidenceSummary]:
        since_utc = _aware_utc(since, "since")
        until_utc = _aware_utc(until, "until")
        if until_utc <= since_utc:
            raise ValueError("until must be after since")

        event = await EventRepository(self.session).resolve_event(event_reference)
        associated_bvids = set(
            await self.session.scalars(
                select(EventVideo.bvid).where(
                    EventVideo.event_id == event.id,
                    EventVideo.active.is_(True),
                )
            )
        )
        if bvid is not None and bvid not in associated_bvids:
            raise ValueError(f"Video is not associated with event {event.slug}: {bvid}")
        selected_bvids = [bvid] if bvid is not None else sorted(associated_bvids)

        observations: list[CommentObservation] = []
        if selected_bvids:
            observations = list(
                await self.session.scalars(
                    select(CommentObservation).where(
                        CommentObservation.bvid.in_(selected_bvids),
                        CommentObservation.captured_at >= since_utc,
                        CommentObservation.captured_at < until_utc,
                        CommentObservation.content.is_not(None),
                    )
                )
            )

        distinct: dict[str, set[tuple[str, int]]] = {
            category: set() for category in STANCE_CATEGORIES
        }
        observation_counts = dict.fromkeys(STANCE_CATEGORIES, 0)
        term_counts: dict[str, dict[str, int]] = {
            category: {} for category in STANCE_CATEGORIES
        }
        for observation in observations:
            content = _normalize_text(observation.content or "")
            for category in STANCE_CATEGORIES:
                matched_terms = [
                    term for term in lexicon.terms[category] if term in content
                ]
                if not matched_terms:
                    continue
                distinct[category].add((observation.bvid, observation.rpid))
                observation_counts[category] += 1
                for term in matched_terms:
                    term_counts[category][term] = term_counts[category].get(term, 0) + 1

        scope_type = "video" if bvid is not None else "event"
        scope_id = bvid or event.slug
        return [
            StanceEvidenceSummary(
                event_id=event.id,
                event_slug=event.slug,
                scope_type=scope_type,
                scope_id=scope_id,
                since=since_utc,
                until=until_utc,
                lexicon_version=lexicon.version,
                category=category,
                lexicon_terms=lexicon.terms[category],
                distinct_comment_count=len(distinct[category]),
                observation_count=observation_counts[category],
                matched_term_counts=term_counts[category],
            )
            for category in STANCE_CATEGORIES
        ]


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value.astimezone(UTC)
