from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import (
    CollectionPolicyVersion,
    KnownVideo,
    VideoCollectionState,
)
from books_of_time.domain.cohort_policy import (
    TierAssessment,
    VideoLifeStage,
)


class CollectionPolicyVersionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        version: str,
        policy_kind: str,
        scope_type: str,
        scope_id: str | None,
        timezone: str,
        policy: Mapping[str, Any],
        algorithm: str,
        created_at: datetime,
        training_window_start: datetime | None = None,
        training_window_end: datetime | None = None,
        distinct_comment_count: int = 0,
        complete_day_count: int = 0,
        valid_exposure_minutes: int = 0,
        excluded_comment_count: int = 0,
        exclusion_reasons: Mapping[str, Any] | None = None,
    ) -> CollectionPolicyVersion:
        normalized_scope_type, normalized_scope_id = _normalize_scope(
            scope_type,
            scope_id,
        )
        row = CollectionPolicyVersion(
            version=_required_text(version, "version"),
            policy_kind=_required_text(policy_kind, "policy_kind"),
            scope_type=normalized_scope_type,
            scope_id=normalized_scope_id,
            timezone=_required_text(timezone, "timezone"),
            policy=deepcopy(dict(policy)),
            training_window_start=training_window_start,
            training_window_end=training_window_end,
            distinct_comment_count=distinct_comment_count,
            complete_day_count=complete_day_count,
            valid_exposure_minutes=valid_exposure_minutes,
            excluded_comment_count=excluded_comment_count,
            exclusion_reasons=deepcopy(dict(exclusion_reasons or {})),
            algorithm=_required_text(algorithm, "algorithm"),
            created_at=created_at,
            active=False,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def activate(
        self,
        version: str,
        *,
        activated_at: datetime,
    ) -> CollectionPolicyVersion:
        target = await self.session.scalar(
            select(CollectionPolicyVersion)
            .where(CollectionPolicyVersion.version == version)
            .with_for_update()
        )
        if target is None:
            raise ValueError(f"Unknown collection policy version: {version}")
        if target.active:
            return target

        active_rows = (
            await self.session.scalars(
                select(CollectionPolicyVersion)
                .where(
                    CollectionPolicyVersion.policy_kind == target.policy_kind,
                    CollectionPolicyVersion.scope_type == target.scope_type,
                    CollectionPolicyVersion.scope_id == target.scope_id,
                    CollectionPolicyVersion.active.is_(True),
                )
                .with_for_update()
            )
        ).all()
        for active in active_rows:
            active.active = False
            active.superseded_at = activated_at
        if active_rows:
            await self.session.flush()

        target.active = True
        target.activated_at = activated_at
        target.superseded_at = None
        await self.session.flush()
        return target

    async def get_active(
        self,
        *,
        policy_kind: str,
        scope_type: str,
        scope_id: str | None,
    ) -> CollectionPolicyVersion | None:
        normalized_scope_type, normalized_scope_id = _normalize_scope(
            scope_type,
            scope_id,
        )
        return await self.session.scalar(
            select(CollectionPolicyVersion).where(
                CollectionPolicyVersion.policy_kind
                == _required_text(policy_kind, "policy_kind"),
                CollectionPolicyVersion.scope_type == normalized_scope_type,
                CollectionPolicyVersion.scope_id == normalized_scope_id,
                CollectionPolicyVersion.active.is_(True),
            )
        )


class VideoCollectionStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def adopt(
        self,
        *,
        bvid: str,
        policy_version: str,
        adopted_at: datetime,
    ) -> VideoCollectionState:
        normalized_bvid = _required_text(bvid, "bvid")
        existing = await self.session.get(VideoCollectionState, normalized_bvid)
        if existing is not None:
            return existing

        video = await self.session.get(KnownVideo, normalized_bvid)
        if video is None:
            raise ValueError(f"Unknown known video: {normalized_bvid}")
        state = VideoCollectionState(
            bvid=normalized_bvid,
            desired_tier="c",
            effective_tier="c",
            candidate_downgrade_tier=None,
            consecutive_downgrade_count=0,
            pinned_tier=None,
            life_stage="active",
            schedule_anchor_at=video.pubdate,
            next_due_at=None,
            last_planned_at=None,
            last_completed_cohort_at=None,
            last_checkpoint_hours=None,
            policy_version=_required_text(policy_version, "policy_version"),
            extra={},
            created_at=adopted_at,
            updated_at=adopted_at,
        )
        self.session.add(state)
        await self.session.flush()
        return state

    async def apply_assessment(
        self,
        *,
        bvid: str,
        assessment: TierAssessment,
        life_stage: VideoLifeStage,
        policy_version: str,
        next_due_at: datetime | None,
        updated_at: datetime,
    ) -> VideoCollectionState:
        normalized_bvid = _required_text(bvid, "bvid")
        state = await self.session.get(VideoCollectionState, normalized_bvid)
        if state is None:
            raise ValueError(
                f"Video collection state does not exist: {normalized_bvid}"
            )

        state.desired_tier = assessment.desired.value
        state.effective_tier = assessment.effective.value
        state.candidate_downgrade_tier = (
            assessment.candidate_downgrade.value
            if assessment.candidate_downgrade is not None
            else None
        )
        state.consecutive_downgrade_count = assessment.consecutive_downgrade_count
        state.life_stage = life_stage.value
        state.policy_version = _required_text(policy_version, "policy_version")
        state.next_due_at = next_due_at
        state.updated_at = updated_at
        await self.session.flush()
        return state


def _normalize_scope(scope_type: str, scope_id: str | None) -> tuple[str, str]:
    normalized_type = _required_text(scope_type, "scope_type").casefold()
    normalized_id = str(scope_id).strip() if scope_id is not None else ""
    if normalized_type == "global":
        if normalized_id not in {"", "global"}:
            raise ValueError("global policy scope_id must be empty or 'global'")
        return "global", "global"
    if normalized_type == "game":
        if not normalized_id:
            raise ValueError("game policy scope_id must not be empty")
        return "game", normalized_id
    raise ValueError("policy scope_type must be 'global' or 'game'")


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized
