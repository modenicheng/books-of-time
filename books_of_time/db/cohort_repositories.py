from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import (
    CollectionPolicyVersion,
    CollectionTask,
    KnownVideo,
    SnapshotCohort,
    SnapshotCohortComponent,
    VideoCollectionState,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.cohort_policy import (
    CohortComponentStatus,
    CohortRolloutMode,
    CohortStatus,
    CollectionTier,
    TierAssessment,
    VideoLifeStage,
    component_key,
)
from books_of_time.domain.enums import TaskKind


@dataclass(frozen=True, slots=True)
class CohortComponentPlan:
    component_kind: str
    task_kind: TaskKind | None
    planned_pages: int
    required: bool = True
    status: CohortComponentStatus = CohortComponentStatus.PENDING
    priority: int = 0
    budget_cost: int = 1
    payload: Mapping[str, Any] = field(default_factory=dict)
    not_before: datetime | None = None
    deadline: datetime | None = None
    max_retries: int = 3
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SnapshotCohortPlan:
    cohort_key: str
    bvid: str
    scheduled_for: datetime
    reason: str
    age_checkpoint_hours: int | None
    desired_tier: CollectionTier
    effective_tier: CollectionTier
    policy_version: str
    deadline: datetime | None
    status: CohortStatus
    status_reason: str | None
    extra: Mapping[str, Any]
    components: tuple[CohortComponentPlan, ...]


@dataclass(frozen=True, slots=True)
class CohortMaterializationResult:
    cohort: SnapshotCohort
    components: tuple[SnapshotCohortComponent, ...]
    tasks: tuple[CollectionTask, ...]
    cohort_created: bool
    components_created: int
    tasks_created: int


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


class SnapshotCohortRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def materialize(
        self,
        plan: SnapshotCohortPlan,
        *,
        rollout_mode: CohortRolloutMode,
        now: datetime,
    ) -> CohortMaterializationResult:
        _validate_cohort_plan(plan)
        _require_aware(now, "now")

        state = await self.session.scalar(
            select(VideoCollectionState)
            .where(VideoCollectionState.bvid == plan.bvid)
            .with_for_update()
        )
        if state is None:
            raise ValueError(f"Video collection state does not exist: {plan.bvid}")

        cohort, cohort_created = await self._get_or_create_cohort(
            plan,
            rollout_mode=rollout_mode,
            now=now,
        )
        _validate_existing_cohort(cohort, plan, rollout_mode)

        existing_components = {
            component.component_kind: component
            for component in await self.session.scalars(
                select(SnapshotCohortComponent)
                .where(SnapshotCohortComponent.cohort_id == cohort.id)
                .order_by(SnapshotCohortComponent.id.asc())
                .with_for_update()
            )
        }
        missing_kinds = {
            component_plan.component_kind
            for component_plan in plan.components
            if component_plan.component_kind not in existing_components
        }
        if missing_kinds and not cohort_created and cohort.reason != "recovery":
            raise ValueError(
                "non-recovery cohort cannot add components after materialization"
            )

        components_created = 0
        ordered_components: list[SnapshotCohortComponent] = []
        for component_plan in plan.components:
            component = existing_components.get(component_plan.component_kind)
            if component is None:
                component, created = await self._get_or_create_component(
                    cohort,
                    plan,
                    component_plan,
                )
                components_created += int(created)
                existing_components[component.component_kind] = component
            _validate_existing_component(component, plan, component_plan)
            ordered_components.append(component)

        if cohort.reason == "recovery":
            cohort.extra = {
                **cohort.extra,
                **deepcopy(dict(plan.extra)),
            }
        cohort.expected_component_count = len(existing_components)
        cohort.completed_component_count = sum(
            component.status
            in {
                CohortComponentStatus.COMPLETE.value,
                CohortComponentStatus.NOT_APPLICABLE.value,
            }
            for component in existing_components.values()
        )
        cohort.updated_at = now

        tasks: list[CollectionTask] = []
        tasks_created = 0
        if (
            rollout_mode is CohortRolloutMode.LIVE
            and plan.status is CohortStatus.PLANNED
        ):
            task_repository = CollectionTaskRepository(self.session)
            for component_plan, component in zip(
                plan.components,
                ordered_components,
                strict=True,
            ):
                if (
                    component_plan.status is not CohortComponentStatus.PENDING
                    or component_plan.task_kind is None
                ):
                    continue
                existing_task = await self.session.scalar(
                    select(CollectionTask)
                    .where(CollectionTask.snapshot_cohort_component_id == component.id)
                    .order_by(CollectionTask.id.asc())
                    .limit(1)
                    .with_for_update()
                )
                if existing_task is not None:
                    tasks.append(existing_task)
                    continue

                not_before = component_plan.not_before or max(
                    now,
                    plan.scheduled_for,
                )
                task = await task_repository.enqueue(
                    kind=component_plan.task_kind,
                    target_type="video",
                    target_id=plan.bvid,
                    priority=component_plan.priority,
                    budget_cost=component_plan.budget_cost,
                    payload={
                        **deepcopy(dict(component_plan.payload)),
                        "bvid": plan.bvid,
                        "reason": plan.reason,
                        "scheduled_for": plan.scheduled_for.isoformat(),
                        "cohort_key": plan.cohort_key,
                        "component_kind": component_plan.component_kind,
                    },
                    not_before=not_before,
                    max_retries=component_plan.max_retries,
                    idempotency_key=component_key(
                        plan.cohort_key,
                        component_plan.component_kind,
                    ),
                    snapshot_cohort_id=cohort.id,
                    snapshot_cohort_component_id=component.id,
                )
                tasks.append(task)
                tasks_created += 1

        await self.session.flush()
        return CohortMaterializationResult(
            cohort=cohort,
            components=tuple(ordered_components),
            tasks=tuple(tasks),
            cohort_created=cohort_created,
            components_created=components_created,
            tasks_created=tasks_created,
        )

    async def _get_or_create_cohort(
        self,
        plan: SnapshotCohortPlan,
        *,
        rollout_mode: CohortRolloutMode,
        now: datetime,
    ) -> tuple[SnapshotCohort, bool]:
        existing = await self.session.scalar(
            select(SnapshotCohort)
            .where(SnapshotCohort.cohort_key == plan.cohort_key)
            .with_for_update()
        )
        if existing is not None:
            return existing, False

        status = (
            CohortStatus.SHADOW_PLANNED
            if rollout_mode is CohortRolloutMode.SHADOW
            else plan.status
        )
        extra = {
            **deepcopy(dict(plan.extra)),
            "rollout_mode": rollout_mode.value,
        }
        if rollout_mode is CohortRolloutMode.SHADOW:
            extra["shadow_target_status"] = plan.status.value
        row = SnapshotCohort(
            cohort_key=_required_text(plan.cohort_key, "cohort_key"),
            bvid=_required_text(plan.bvid, "bvid"),
            scheduled_for=plan.scheduled_for,
            reason=_required_text(plan.reason, "reason"),
            age_checkpoint_hours=plan.age_checkpoint_hours,
            desired_tier=plan.desired_tier.value,
            effective_tier=plan.effective_tier.value,
            policy_version=_required_text(plan.policy_version, "policy_version"),
            deadline=plan.deadline,
            status=status.value,
            status_reason=plan.status_reason,
            started_at=None,
            finished_at=now if _terminal_cohort_status(status) else None,
            expected_component_count=0,
            completed_component_count=0,
            extra=extra,
            created_at=now,
            updated_at=now,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
            return row, True
        except IntegrityError:
            existing = await self.session.scalar(
                select(SnapshotCohort)
                .where(SnapshotCohort.cohort_key == plan.cohort_key)
                .with_for_update()
            )
            if existing is None:
                raise
            return existing, False

    async def _get_or_create_component(
        self,
        cohort: SnapshotCohort,
        plan: SnapshotCohortPlan,
        component_plan: CohortComponentPlan,
    ) -> tuple[SnapshotCohortComponent, bool]:
        extra = {
            **deepcopy(dict(component_plan.extra)),
            "task_kind": (
                component_plan.task_kind.value
                if component_plan.task_kind is not None
                else None
            ),
        }
        row = SnapshotCohortComponent(
            cohort_id=cohort.id,
            component_kind=_required_text(
                component_plan.component_kind,
                "component_kind",
            ),
            required=component_plan.required,
            status=component_plan.status.value,
            scheduled_for=plan.scheduled_for,
            deadline=component_plan.deadline or plan.deadline,
            started_at=None,
            finished_at=None,
            skew_seconds=None,
            planned_pages=component_plan.planned_pages,
            requested_pages=0,
            succeeded_pages=0,
            items_observed=0,
            raw_payloads_saved=0,
            comment_scan_run_id=None,
            failure_reason=None,
            extra=extra,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
            return row, True
        except IntegrityError:
            existing = await self.session.scalar(
                select(SnapshotCohortComponent)
                .where(
                    SnapshotCohortComponent.cohort_id == cohort.id,
                    SnapshotCohortComponent.component_kind
                    == component_plan.component_kind,
                )
                .with_for_update()
            )
            if existing is None:
                raise
            return existing, False


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


def _validate_cohort_plan(plan: SnapshotCohortPlan) -> None:
    _required_text(plan.cohort_key, "cohort_key")
    _required_text(plan.bvid, "bvid")
    _required_text(plan.reason, "reason")
    _required_text(plan.policy_version, "policy_version")
    _require_aware(plan.scheduled_for, "scheduled_for")
    if plan.deadline is not None:
        _require_aware(plan.deadline, "deadline")
        if plan.deadline <= plan.scheduled_for:
            raise ValueError("cohort deadline must be after scheduled_for")
    if plan.age_checkpoint_hours is not None and plan.age_checkpoint_hours <= 0:
        raise ValueError("age_checkpoint_hours must be positive")
    if not plan.components:
        raise ValueError("cohort plan must contain at least one component")
    component_kinds: set[str] = set()
    for component in plan.components:
        normalized_kind = _required_text(component.component_kind, "component_kind")
        if normalized_kind in component_kinds:
            raise ValueError(f"duplicate cohort component: {normalized_kind}")
        component_kinds.add(normalized_kind)
        if component.planned_pages < 0:
            raise ValueError("component planned_pages must be non-negative")
        if component.budget_cost <= 0:
            raise ValueError("component budget_cost must be positive")
        if component.max_retries < 0:
            raise ValueError("component max_retries must be non-negative")
        if component.not_before is not None:
            _require_aware(component.not_before, "component not_before")
        if component.deadline is not None:
            _require_aware(component.deadline, "component deadline")


def _validate_existing_cohort(
    cohort: SnapshotCohort,
    plan: SnapshotCohortPlan,
    rollout_mode: CohortRolloutMode,
) -> None:
    identity = (
        cohort.bvid,
        cohort.scheduled_for,
        cohort.reason,
        cohort.age_checkpoint_hours,
        cohort.desired_tier,
        cohort.effective_tier,
        cohort.policy_version,
        cohort.deadline,
    )
    planned_identity = (
        plan.bvid,
        plan.scheduled_for,
        plan.reason,
        plan.age_checkpoint_hours,
        plan.desired_tier.value,
        plan.effective_tier.value,
        plan.policy_version,
        plan.deadline,
    )
    if identity != planned_identity:
        raise ValueError(f"cohort key identity conflict: {plan.cohort_key}")
    if cohort.extra.get("rollout_mode") != rollout_mode.value:
        raise ValueError(f"cohort key rollout conflict: {plan.cohort_key}")


def _validate_existing_component(
    component: SnapshotCohortComponent,
    plan: SnapshotCohortPlan,
    component_plan: CohortComponentPlan,
) -> None:
    task_kind = (
        component_plan.task_kind.value if component_plan.task_kind is not None else None
    )
    if (
        component.required != component_plan.required
        or component.scheduled_for != plan.scheduled_for
        or component.deadline != (component_plan.deadline or plan.deadline)
        or component.planned_pages != component_plan.planned_pages
        or component.extra.get("task_kind") != task_kind
    ):
        raise ValueError(
            f"component plan conflict: {plan.cohort_key}:{component_plan.component_kind}"
        )


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _terminal_cohort_status(status: CohortStatus) -> bool:
    return status in {
        CohortStatus.SHADOW_PLANNED,
        CohortStatus.COMPLETE,
        CohortStatus.PARTIAL,
        CohortStatus.MISSED,
        CohortStatus.CORRUPTED,
        CohortStatus.NOT_APPLICABLE,
    }
