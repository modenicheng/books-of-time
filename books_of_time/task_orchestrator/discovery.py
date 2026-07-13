from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.db.models import KnownVideo
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    EventRepository,
    KnownVideoSourceRepository,
)
from books_of_time.domain.enums import TaskKind


@dataclass(frozen=True)
class DiscoveredVideo:
    bvid: str
    pubdate: datetime
    source_mid: str
    source_pool_type: str | None = None
    source_pool_id: str | None = None
    source_associations: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class EventDiscoveryLink:
    event_id: int
    target_id: int

    def as_payload(self) -> dict[str, int]:
        return {"event_id": self.event_id, "target_id": self.target_id}


class DiscoveryScheduler:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        fresh_video_window: timedelta = timedelta(minutes=2),
    ) -> None:
        self.session_factory = session_factory
        self.fresh_video_window = fresh_video_window

    async def handle_discovered_videos(
        self,
        *,
        session: AsyncSession,
        videos: list[DiscoveredVideo],
        event_links: list[EventDiscoveryLink] | None = None,
        source_associations: list[dict[str, Any]] | None = None,
        raw_page_observation_id: int | None = None,
        now: datetime,
    ) -> list[str]:
        created: list[str] = []
        repo = CollectionTaskRepository(session)
        event_repo = EventRepository(session)
        source_repo = KnownVideoSourceRepository(session)
        valid_event_links: list[EventDiscoveryLink] = []
        for link in event_links or []:
            target = await event_repo.resolve_active_uid_target(
                event_id=link.event_id,
                target_id=link.target_id,
                now=now,
            )
            if target is not None:
                valid_event_links.append(link)

        for video in videos:
            normalized_associations = normalize_source_associations(
                source_mid=video.source_mid,
                source_pool_type=video.source_pool_type,
                source_pool_id=video.source_pool_id,
                source_associations=(
                    source_associations
                    if source_associations is not None
                    else list(video.source_associations) or None
                ),
            )
            primary_source = normalized_associations[0]
            existing = await session.scalar(
                select(KnownVideo).where(KnownVideo.bvid == video.bvid)
            )
            if existing is None:
                session.add(
                    KnownVideo(
                        bvid=video.bvid,
                        source_mid=str(primary_source["source_mid"]),
                        pubdate=video.pubdate,
                        first_seen_at=now,
                    )
                )
                await session.flush()

            await source_repo.upsert_for_video(
                bvid=video.bvid,
                associations=normalized_associations,
                seen_at=now,
                raw_page_observation_id=raw_page_observation_id,
            )

            if existing is None:
                is_delayed_discovery = now - video.pubdate > self.fresh_video_window

                await repo.enqueue(
                    kind=TaskKind.FETCH_VIDEO_STATS,
                    target_type="video",
                    target_id=video.bvid,
                    priority=90 if is_delayed_discovery else 100,
                    payload={
                        "bvid": video.bvid,
                        "source_mid": primary_source["source_mid"],
                        "reason": "delayed_discovery"
                        if is_delayed_discovery
                        else "fresh_discovery",
                        "source_pool_type": primary_source["pool_type"],
                        "source_pool_id": primary_source["pool_id"],
                        "source_associations": normalized_associations,
                        "event_links": [
                            link.as_payload() for link in valid_event_links
                        ],
                    },
                    not_before=now,
                    idempotency_key=(
                        f"{TaskKind.FETCH_VIDEO_STATS.value}:video:"
                        f"{video.bvid}:discovery"
                    ),
                )
                created.append(video.bvid)

            for link in valid_event_links:
                await event_repo.attach_video(
                    event_id=link.event_id,
                    bvid=video.bvid,
                    source_target_id=link.target_id,
                    association_reason="uid_target",
                    confidence=1.0,
                    now=now,
                )

        return created


def normalize_source_associations(
    *,
    source_mid: str,
    source_pool_type: str | None = None,
    source_pool_id: str | None = None,
    source_associations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    fallback_mid = _required_source_text(source_mid, "source_mid")
    if source_associations is None:
        pool_type = _optional_source_text(source_pool_type) or "matrix"
        pool_id = _optional_source_text(source_pool_id) or pool_type
        source_associations = [
            {
                "source_mid": fallback_mid,
                "pool_type": pool_type,
                "pool_id": pool_id,
                "game_id": pool_id if pool_type == "game" else None,
                "official": False,
                "monitored": True,
            }
        ]

    normalized: dict[
        tuple[str, str, str, str, bool, bool],
        dict[str, Any],
    ] = {}
    for association in source_associations:
        if not isinstance(association, dict):
            raise ValueError("Discovery source association must be a mapping")
        association_mid = _required_source_text(
            association.get("source_mid", fallback_mid),
            "source_mid",
        )
        pool_type = _required_source_text(
            association.get("pool_type", source_pool_type or "matrix"),
            "pool_type",
        )
        pool_id = _required_source_text(
            association.get("pool_id") or source_pool_id or pool_type,
            "pool_id",
        )
        game_id = _optional_source_text(association.get("game_id"))
        official = _source_bool(association, "official", False)
        monitored = _source_bool(association, "monitored", True)
        identity = (
            association_mid,
            pool_type,
            pool_id,
            game_id or "",
            official,
            monitored,
        )
        normalized.setdefault(
            identity,
            {
                "source_mid": association_mid,
                "pool_type": pool_type,
                "pool_id": pool_id,
                "game_id": game_id,
                "official": official,
                "monitored": monitored,
            },
        )
    if not normalized:
        raise ValueError("Discovery source associations must not be empty")
    return [normalized[key] for key in sorted(normalized)]


def _required_source_text(value: object, field_name: str) -> str:
    normalized = _optional_source_text(value)
    if normalized is None:
        raise ValueError(f"Discovery source {field_name} must not be empty")
    return normalized


def _optional_source_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _source_bool(
    association: dict[str, Any],
    field_name: str,
    default: bool,
) -> bool:
    value = association.get(field_name, default)
    if not isinstance(value, bool):
        raise ValueError(f"Discovery source {field_name} must be a boolean")
    return value
