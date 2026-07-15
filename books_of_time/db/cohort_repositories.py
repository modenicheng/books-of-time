from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from books_of_time.db.comment_scan_repositories import (
    CommentScanRunRepository,
    HotScanRunPlan,
)
from books_of_time.db.latest_scan_repositories import (
    LatestScanClaim,
    LatestScanRunPlan,
    LatestScanRunRepository,
)
from books_of_time.db.models import (
    CollectionCoverageStat,
    CollectionPolicyVersion,
    CollectionScheduleGap,
    CollectionTask,
    CommentScanRun,
    FrontierState,
    HttpRequestAttempt,
    KnownVideo,
    SnapshotCohort,
    SnapshotCohortComponent,
    VideoCollectionState,
)
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    FrontierStateRepository,
    FrontierStateUpdate,
    FrontierVersionConflict,
)
from books_of_time.domain.cohort_policy import (
    CohortComponentStatus,
    CohortPolicy,
    CohortRolloutMode,
    CohortStatus,
    CollectionTier,
    ComponentOutcome,
    TierAssessment,
    VideoLifeStage,
    aggregate_cohort_status,
    component_key,
)
from books_of_time.domain.enums import (
    CommentScanMode,
    CommentScanStatus,
    TaskKind,
)
from books_of_time.domain.latest_frontier import normalize_anchor_set, primary_anchor


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

    async def ensure_configured(
        self,
        policy: CohortPolicy,
        *,
        now: datetime,
    ) -> CollectionPolicyVersion:
        persisted_policy = policy.as_persisted_policy()
        target = await self.session.scalar(
            select(CollectionPolicyVersion)
            .where(CollectionPolicyVersion.version == policy.policy_version)
            .with_for_update()
        )
        if target is None:
            try:
                async with self.session.begin_nested():
                    target = await self.create(
                        version=policy.policy_version,
                        policy_kind="snapshot_cohort",
                        scope_type="global",
                        scope_id="global",
                        timezone=policy.timezone.key,
                        policy=persisted_policy,
                        algorithm="configured-fixed-v1",
                        created_at=now,
                    )
            except IntegrityError:
                target = await self.session.scalar(
                    select(CollectionPolicyVersion)
                    .where(CollectionPolicyVersion.version == policy.policy_version)
                    .with_for_update()
                )
                if target is None:
                    raise

        expected_identity = (
            "snapshot_cohort",
            "global",
            "global",
            policy.timezone.key,
            persisted_policy,
            "configured-fixed-v1",
        )
        stored_identity = (
            target.policy_kind,
            target.scope_type,
            target.scope_id,
            target.timezone,
            target.policy,
            target.algorithm,
        )
        if stored_identity != expected_identity:
            raise ValueError(
                "Configured cohort policy content differs from the immutable "
                f"version {policy.policy_version}; choose a new policy_version"
            )
        return await self.activate(policy.policy_version, activated_at=now)


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

    async def list_candidates(self, *, limit: int = 5000) -> list[KnownVideo]:
        if limit <= 0:
            raise ValueError("candidate limit must be positive")
        adoption_rank = case(
            (VideoCollectionState.bvid.is_(None), 0),
            else_=1,
        )
        missing_due_rank = case(
            (VideoCollectionState.next_due_at.is_(None), 1),
            else_=0,
        )
        return list(
            await self.session.scalars(
                select(KnownVideo)
                .outerjoin(
                    VideoCollectionState,
                    VideoCollectionState.bvid == KnownVideo.bvid,
                )
                .order_by(
                    adoption_rank.asc(),
                    missing_due_rank.asc(),
                    VideoCollectionState.next_due_at.asc(),
                    KnownVideo.first_seen_at.asc(),
                    KnownVideo.bvid.asc(),
                )
                .limit(limit)
            )
        )

    async def lock(self, bvid: str) -> VideoCollectionState | None:
        return await self.session.scalar(
            select(VideoCollectionState)
            .where(VideoCollectionState.bvid == _required_text(bvid, "bvid"))
            .with_for_update()
        )

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

    async def record_planning(
        self,
        *,
        bvid: str,
        assessment: TierAssessment,
        life_stage: VideoLifeStage,
        policy_version: str,
        next_due_at: datetime | None,
        last_planned_at: datetime,
        last_checkpoint_hours: int | None,
        updated_at: datetime,
    ) -> VideoCollectionState:
        state = await self.apply_assessment(
            bvid=bvid,
            assessment=assessment,
            life_stage=life_stage,
            policy_version=policy_version,
            next_due_at=next_due_at,
            updated_at=updated_at,
        )
        state.last_planned_at = last_planned_at
        state.last_checkpoint_hours = last_checkpoint_hours
        await self.session.flush()
        return state


class CollectionScheduleGapRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        bvid: str,
        gap_start: datetime,
        gap_end: datetime,
        expected_cohort_count: int,
        reason: str,
        policy_version: str,
        created_at: datetime,
        service_instance_id: str | None = None,
    ) -> tuple[CollectionScheduleGap, bool]:
        if gap_end <= gap_start:
            raise ValueError("schedule gap end must be after start")
        if expected_cohort_count <= 0:
            raise ValueError("schedule gap expected count must be positive")
        identity = (
            CollectionScheduleGap.bvid == _required_text(bvid, "bvid"),
            CollectionScheduleGap.gap_start == gap_start,
            CollectionScheduleGap.gap_end == gap_end,
            CollectionScheduleGap.reason == _required_text(reason, "reason"),
            CollectionScheduleGap.policy_version
            == _required_text(policy_version, "policy_version"),
        )
        existing = await self.session.scalar(
            select(CollectionScheduleGap).where(*identity).with_for_update()
        )
        if existing is not None:
            if existing.expected_cohort_count != expected_cohort_count:
                raise ValueError("schedule gap identity has a different expected count")
            return existing, False

        row = CollectionScheduleGap(
            bvid=bvid,
            gap_start=gap_start,
            gap_end=gap_end,
            expected_cohort_count=expected_cohort_count,
            reason=reason,
            service_instance_id=service_instance_id,
            policy_version=policy_version,
            created_at=created_at,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
            return row, True
        except IntegrityError:
            existing = await self.session.scalar(
                select(CollectionScheduleGap).where(*identity).with_for_update()
            )
            if existing is None:
                raise
            if existing.expected_cohort_count != expected_cohort_count:
                raise ValueError("schedule gap identity has a different expected count")
            return existing, False


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
            scan_repository = CommentScanRunRepository(self.session)
            latest_scan_repository = LatestScanRunRepository(self.session)
            frontier_repository = FrontierStateRepository(self.session)
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
                if _is_managed_latest_component(component_plan):
                    existing_task_statement = select(CollectionTask).where(
                        CollectionTask.snapshot_cohort_component_id == component.id
                    )
                    if component.comment_scan_run_id is not None:
                        existing_task_statement = existing_task_statement.where(
                            CollectionTask.comment_scan_run_id
                            == component.comment_scan_run_id
                        )
                    existing_latest_task = await self.session.scalar(
                        existing_task_statement.order_by(CollectionTask.id.asc())
                        .limit(1)
                        .with_for_update()
                    )
                    if existing_latest_task is not None:
                        if component.comment_scan_run_id is None:
                            raise ValueError(
                                "Existing latest task has no linked comment scan run"
                            )
                        existing_scan = await self.session.scalar(
                            select(CommentScanRun)
                            .where(CommentScanRun.id == component.comment_scan_run_id)
                            .with_for_update()
                        )
                        if existing_scan is None:
                            raise LookupError(
                                "Existing latest task comment scan run was not found"
                            )
                        _validate_existing_latest_task(
                            existing_latest_task,
                            scan_run_id=existing_scan.id,
                            scan_mode=existing_scan.mode,
                        )
                        tasks.append(existing_latest_task)
                        continue
                scan = None
                latest_claim: LatestScanClaim | None = None
                if _is_managed_hot_component(component_plan):
                    scan, _created = await scan_repository.materialize_hot(
                        _hot_scan_run_plan(
                            plan,
                            cohort=cohort,
                            component_plan=component_plan,
                        ),
                        now=now,
                    )
                    if component.comment_scan_run_id is None:
                        component.comment_scan_run_id = scan.id
                    elif component.comment_scan_run_id != scan.id:
                        raise ValueError(
                            "Cohort component belongs to another comment scan run"
                        )
                elif _is_managed_latest_component(component_plan):
                    frontier = await frontier_repository.get_or_create(
                        target_type="video",
                        target_id=plan.bvid,
                        frontier_type="latest_comments",
                        now=now,
                        lock=True,
                    )
                    latest_claim = await latest_scan_repository.claim_or_join(
                        _latest_scan_run_plan(
                            plan,
                            cohort=cohort,
                            component_plan=component_plan,
                            frontier=frontier,
                        ),
                        frontier_state=frontier,
                        expected_version=frontier.version,
                        now=now,
                    )
                    scan = latest_claim.scan
                    if component.comment_scan_run_id is None:
                        component.comment_scan_run_id = scan.id
                    elif component.comment_scan_run_id != scan.id:
                        raise ValueError(
                            "Cohort component belongs to another comment scan run"
                        )
                existing_task = await self.session.scalar(
                    select(CollectionTask)
                    .where(CollectionTask.snapshot_cohort_component_id == component.id)
                    .order_by(CollectionTask.id.asc())
                    .limit(1)
                    .with_for_update()
                )
                if existing_task is not None:
                    if scan is not None:
                        if latest_claim is None:
                            _validate_existing_hot_task(
                                existing_task,
                                scan_run_id=scan.id,
                                scan_mode=scan.mode,
                            )
                        else:
                            _validate_existing_latest_task(
                                existing_task,
                                scan_run_id=scan.id,
                                scan_mode=scan.mode,
                            )
                    tasks.append(existing_task)
                    continue

                if latest_claim is not None and not latest_claim.created:
                    component.status = CohortComponentStatus.JOINED_ACTIVE_TASK.value
                    continue

                not_before = component_plan.not_before or max(
                    now,
                    plan.scheduled_for,
                )
                scan_slice_key = (
                    f"{scan.id}:{scan.mode.value}:0" if scan is not None else None
                )
                task_payload = {
                    **deepcopy(dict(component_plan.payload)),
                    "bvid": plan.bvid,
                    "reason": plan.reason,
                    "scheduled_for": plan.scheduled_for.isoformat(),
                    "cohort_key": plan.cohort_key,
                    "component_kind": component_plan.component_kind,
                }
                if latest_claim is not None:
                    task_payload.update(
                        {
                            "scan_mode": scan.mode.value,
                            "frontier_version": latest_claim.frontier_state.version,
                            "current_head_required": True,
                        }
                    )
                task = await task_repository.enqueue(
                    kind=component_plan.task_kind,
                    target_type="video",
                    target_id=plan.bvid,
                    priority=component_plan.priority,
                    budget_cost=component_plan.budget_cost,
                    payload=task_payload,
                    not_before=not_before,
                    max_retries=component_plan.max_retries,
                    idempotency_key=(
                        scan_slice_key
                        if latest_claim is not None
                        else component_key(
                            plan.cohort_key,
                            component_plan.component_kind,
                        )
                    ),
                    snapshot_cohort_id=cohort.id,
                    snapshot_cohort_component_id=component.id,
                    comment_scan_run_id=scan.id if scan is not None else None,
                    scan_slice_no=0 if scan is not None else None,
                    scan_slice_key=scan_slice_key,
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

    async def repair_latest_tail_handoffs(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> int:
        _require_aware(now, "now")
        if limit <= 0:
            raise ValueError("repair limit must be positive")
        child_scan = aliased(CommentScanRun)
        child_task = aliased(CollectionTask)
        parents = list(
            await self.session.scalars(
                select(CommentScanRun)
                .outerjoin(
                    child_scan,
                    child_scan.scan_key
                    == CommentScanRun.scan_key + literal(":baseline_head_sweep"),
                )
                .outerjoin(
                    child_task,
                    and_(
                        child_task.comment_scan_run_id == child_scan.id,
                        child_task.scan_slice_no == 0,
                    ),
                )
                .where(
                    CommentScanRun.mode == CommentScanMode.BASELINE_TAIL,
                    CommentScanRun.status == CommentScanStatus.COMPLETE,
                    CommentScanRun.outcome == "tail_reached",
                    or_(child_scan.id.is_(None), child_task.id.is_(None)),
                )
                .order_by(CommentScanRun.finished_at.asc(), CommentScanRun.id.asc())
                .limit(limit)
                .with_for_update(of=CommentScanRun)
            )
        )
        repository = LatestScanRunRepository(self.session)
        repaired = 0
        for parent in parents:
            if not parent.start_anchor_set:
                continue
            child_key = f"{parent.scan_key}:baseline_head_sweep"
            existing_child = await self.session.scalar(
                select(CommentScanRun).where(CommentScanRun.scan_key == child_key)
            )
            existing_task = None
            if existing_child is not None:
                existing_task = await self.session.scalar(
                    select(CollectionTask).where(
                        CollectionTask.comment_scan_run_id == existing_child.id,
                        CollectionTask.scan_slice_no == 0,
                    )
                )
            if existing_child is not None and existing_task is not None:
                continue
            frontier = await FrontierStateRepository(self.session).get_or_create(
                target_type="video",
                target_id=parent.bvid,
                frontier_type="latest_comments",
                now=now,
                lock=True,
            )
            await repository.complete_tail_and_create_head(
                parent.id,
                frontier_state=frontier,
                expected_version=frontier.version,
                now=now,
            )
            repaired += 1
        await self.session.flush()
        return repaired

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
        if _is_managed_latest_component(component_plan):
            extra["repair_task"] = {
                "priority": component_plan.priority,
                "budget_cost": component_plan.budget_cost,
                "max_retries": component_plan.max_retries,
                "payload": deepcopy(dict(component_plan.payload)),
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


class SnapshotCohortExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def mark_task_started(
        self,
        task: CollectionTask,
        *,
        now: datetime,
    ) -> SnapshotCohortComponent | None:
        linked = await self._load_linked(task)
        if linked is None:
            return None
        cohort, component = linked
        if component.status not in {
            CohortComponentStatus.PENDING.value,
            CohortComponentStatus.RUNNING.value,
            CohortComponentStatus.JOINED_ACTIVE_TASK.value,
        }:
            raise ValueError(
                f"Cohort component is not executable: {component.id}:{component.status}"
            )
        if component.started_at is None:
            component.started_at = now
        component.status = CohortComponentStatus.RUNNING.value
        component.finished_at = None
        cohort.status = CohortStatus.RUNNING.value
        cohort.started_at = cohort.started_at or now
        cohort.finished_at = None
        cohort.updated_at = now
        await self.session.flush()
        return component

    async def record_http_attempt_started(
        self,
        attempt: HttpRequestAttempt,
    ) -> SnapshotCohortComponent | None:
        linked = await self._load_linked_ids(
            snapshot_cohort_id=attempt.snapshot_cohort_id,
            snapshot_cohort_component_id=attempt.snapshot_cohort_component_id,
        )
        if linked is None:
            return None
        _cohort, component = linked
        if attempt.request_started_at is None:
            raise ValueError("Cohort HTTP attempt must have request_started_at")
        if component.skew_seconds is None:
            component.skew_seconds = int(
                (attempt.request_started_at - component.scheduled_for).total_seconds()
            )
            await self.session.flush()
        return component

    async def record_task_succeeded(
        self,
        task: CollectionTask,
        coverage: CollectionCoverageStat,
        *,
        finished_at: datetime,
    ) -> SnapshotCohortComponent | None:
        latest_scan = await self._load_latest_task_scan(task)
        if latest_scan is not None:
            await self.sync_latest_scan_consumers(
                latest_scan.id,
                finished_at=finished_at,
            )
            return await self._task_component(task)

        linked = await self._load_linked(task)
        if linked is None:
            return None
        cohort, component = linked
        scan = await self._load_linked_scan(
            task,
            cohort=cohort,
            component=component,
        )
        if scan is not None:
            self._sync_component_from_scan(
                component,
                scan,
                finished_at=finished_at,
            )
        else:
            self._add_coverage(component, coverage)
            if coverage.status == "corrupted":
                component.status = CohortComponentStatus.CORRUPTED.value
                component.failure_reason = coverage.reason or "corrupted"
            elif coverage.status == "partial":
                component.status = CohortComponentStatus.PARTIAL.value
                component.failure_reason = coverage.reason or "partial"
            else:
                component.status = CohortComponentStatus.COMPLETE.value
                component.failure_reason = None
            component.finished_at = finished_at
        await self._recompute_cohort(cohort, finished_at=finished_at)
        return component

    async def record_task_failed(
        self,
        task: CollectionTask,
        coverage: CollectionCoverageStat,
        *,
        terminal: bool,
        finished_at: datetime,
    ) -> SnapshotCohortComponent | None:
        latest_scan = await self._load_latest_task_scan(task)
        if latest_scan is not None:
            component = await self._task_component(task)
            if component is not None:
                component.extra = {
                    **component.extra,
                    "failure_attempts": int(
                        component.extra.get("failure_attempts") or 0
                    )
                    + 1,
                    "last_failure_reason": coverage.reason,
                }
            if terminal:
                await self._terminalize_latest_task_failure(
                    task,
                    latest_scan,
                    coverage,
                    finished_at=finished_at,
                )
            await self.sync_latest_scan_consumers(
                latest_scan.id,
                finished_at=finished_at,
            )
            return await self._task_component(task)

        linked = await self._load_linked(task)
        if linked is None:
            return None
        cohort, component = linked
        scan = await self._load_linked_scan(
            task,
            cohort=cohort,
            component=component,
        )
        component.extra = {
            **component.extra,
            "failure_attempts": int(component.extra.get("failure_attempts") or 0) + 1,
            "last_failure_reason": coverage.reason,
        }
        if scan is not None:
            if terminal and scan.status in {
                CommentScanStatus.PLANNED,
                CommentScanStatus.RUNNING,
                CommentScanStatus.PAUSED,
            }:
                scan = await CommentScanRunRepository(self.session).mark_failed(
                    scan.id,
                    outcome="retry_exhausted",
                    error_type=str(
                        coverage.extra.get("exception_type")
                        or coverage.reason
                        or "collector_error"
                    ),
                    error_message=str(coverage.extra.get("message") or ""),
                    status=(
                        CommentScanStatus.CORRUPTED
                        if coverage.reason == "parse_error"
                        else CommentScanStatus.FAILED
                    ),
                    now=finished_at,
                )
            self._sync_component_from_scan(
                component,
                scan,
                finished_at=finished_at,
            )
            if scan.status in {
                CommentScanStatus.PLANNED,
                CommentScanStatus.RUNNING,
                CommentScanStatus.PAUSED,
            }:
                component.failure_reason = coverage.reason
        else:
            self._add_coverage(component, coverage)
            component.failure_reason = coverage.reason
            if terminal:
                component.status = CohortComponentStatus.FAILED.value
                component.finished_at = finished_at
            else:
                component.status = CohortComponentStatus.RUNNING.value
                component.finished_at = None
        await self._recompute_cohort(cohort, finished_at=finished_at)
        return component

    async def sync_latest_scan_consumers(
        self,
        scan_run_id: int,
        *,
        finished_at: datetime,
    ) -> int:
        _require_aware(finished_at, "finished_at")
        scan = await LatestScanRunRepository(self.session).lock(scan_run_id)
        effective_scan = await self._effective_latest_scan(scan)
        linked_scan_ids = {scan.id, effective_scan.id}
        if effective_scan.parent_scan_run_id is not None:
            linked_scan_ids.add(effective_scan.parent_scan_run_id)

        components = list(
            await self.session.scalars(
                select(SnapshotCohortComponent)
                .where(
                    SnapshotCohortComponent.comment_scan_run_id.in_(linked_scan_ids),
                    SnapshotCohortComponent.component_kind.in_(
                        {"latest_current_head", "latest_reconciliation"}
                    ),
                    SnapshotCohortComponent.status.in_(
                        {
                            CohortComponentStatus.PENDING.value,
                            CohortComponentStatus.RUNNING.value,
                            CohortComponentStatus.JOINED_ACTIVE_TASK.value,
                        }
                    ),
                )
                .order_by(SnapshotCohortComponent.id.asc())
                .with_for_update()
            )
        )
        if not components:
            return 0

        (
            requested,
            succeeded,
            items,
            raw_payloads,
        ) = await self._latest_cumulative_counters(effective_scan)
        head_captured_at = _latest_head_captured_at(effective_scan)
        cohort_ids: set[int] = set()
        for component in components:
            component.comment_scan_run_id = effective_scan.id
            component.requested_pages = requested
            component.succeeded_pages = succeeded
            component.items_observed = items
            component.raw_payloads_saved = raw_payloads
            cohort_ids.add(component.cohort_id)

            if _head_capture_satisfies(component, head_captured_at):
                component.status = CohortComponentStatus.COMPLETE.value
                component.started_at = component.started_at or head_captured_at
                component.finished_at = finished_at
                component.failure_reason = None
            elif effective_scan.status is CommentScanStatus.FAILED:
                component.status = CohortComponentStatus.FAILED.value
                component.finished_at = effective_scan.finished_at or finished_at
                component.failure_reason = (
                    effective_scan.outcome
                    or effective_scan.last_error_type
                    or CommentScanStatus.FAILED.value
                )
            elif effective_scan.status is CommentScanStatus.CORRUPTED:
                component.status = CohortComponentStatus.CORRUPTED.value
                component.finished_at = effective_scan.finished_at or finished_at
                component.failure_reason = (
                    effective_scan.outcome
                    or effective_scan.last_error_type
                    or CommentScanStatus.CORRUPTED.value
                )
            else:
                component.status = CohortComponentStatus.JOINED_ACTIVE_TASK.value
                component.finished_at = None
                component.failure_reason = None

        for cohort_id in sorted(cohort_ids):
            cohort = await self.session.scalar(
                select(SnapshotCohort)
                .where(SnapshotCohort.id == cohort_id)
                .with_for_update()
            )
            if cohort is None:
                raise LookupError(f"Snapshot cohort not found: {cohort_id}")
            await self._recompute_cohort(cohort, finished_at=finished_at)
        return len(components)

    async def repair_latest_consumers(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> int:
        _require_aware(now, "now")
        if isinstance(limit, bool) or limit <= 0:
            raise ValueError("repair limit must be positive")
        component_ids = list(
            await self.session.scalars(
                select(SnapshotCohortComponent.id)
                .join(
                    SnapshotCohort,
                    SnapshotCohort.id == SnapshotCohortComponent.cohort_id,
                )
                .where(
                    SnapshotCohort.status != CohortStatus.SHADOW_PLANNED.value,
                    SnapshotCohortComponent.component_kind.in_(
                        {"latest_current_head", "latest_reconciliation"}
                    ),
                    SnapshotCohortComponent.status.in_(
                        {
                            CohortComponentStatus.PENDING.value,
                            CohortComponentStatus.RUNNING.value,
                            CohortComponentStatus.JOINED_ACTIVE_TASK.value,
                        }
                    ),
                )
                .order_by(
                    SnapshotCohortComponent.deadline.asc(),
                    SnapshotCohortComponent.id.asc(),
                )
                .limit(limit)
            )
        )
        repaired = 0
        synchronized_scan_ids: set[int] = set()
        for component_id in component_ids:
            component = await self.session.scalar(
                select(SnapshotCohortComponent)
                .where(SnapshotCohortComponent.id == component_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if component is None or component.status not in {
                CohortComponentStatus.PENDING.value,
                CohortComponentStatus.RUNNING.value,
                CohortComponentStatus.JOINED_ACTIVE_TASK.value,
            }:
                continue

            scan = await self._component_latest_scan(component)
            if scan is not None and scan.id not in synchronized_scan_ids:
                await self.sync_latest_scan_consumers(scan.id, finished_at=now)
                synchronized_scan_ids.add(scan.id)
                component = await self.session.scalar(
                    select(SnapshotCohortComponent)
                    .where(SnapshotCohortComponent.id == component_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if component is None or component.status not in {
                    CohortComponentStatus.PENDING.value,
                    CohortComponentStatus.RUNNING.value,
                    CohortComponentStatus.JOINED_ACTIVE_TASK.value,
                }:
                    continue
                scan = await self._component_latest_scan(component)

            if component.deadline is not None and now >= component.deadline:
                component.status = CohortComponentStatus.PARTIAL.value
                component.finished_at = now
                component.failure_reason = (
                    "baseline_tail_in_progress"
                    if scan is not None and scan.mode is CommentScanMode.BASELINE_TAIL
                    else "current_head_not_captured"
                )
                cohort = await self._lock_cohort(component.cohort_id)
                await self._recompute_cohort(cohort, finished_at=now)
                repaired += 1
                continue

            if await self._ensure_latest_consumer_active(
                component,
                scan=scan,
                now=now,
            ):
                repaired += 1
        return repaired

    async def _task_component(
        self,
        task: CollectionTask,
    ) -> SnapshotCohortComponent | None:
        linked = await self._load_linked(task)
        return linked[1] if linked is not None else None

    async def _component_latest_scan(
        self,
        component: SnapshotCohortComponent,
    ) -> CommentScanRun | None:
        if component.comment_scan_run_id is None:
            return None
        scan = await self.session.scalar(
            select(CommentScanRun)
            .where(CommentScanRun.id == component.comment_scan_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if scan is None:
            return None
        if scan.mode not in _LATEST_COMMENT_SCAN_MODES:
            raise ValueError("Latest component references an incompatible scan mode")
        return scan

    async def _lock_cohort(self, cohort_id: int) -> SnapshotCohort:
        cohort = await self.session.scalar(
            select(SnapshotCohort)
            .where(SnapshotCohort.id == cohort_id)
            .with_for_update()
        )
        if cohort is None:
            raise LookupError(f"Snapshot cohort not found: {cohort_id}")
        return cohort

    async def _ensure_latest_consumer_active(
        self,
        component: SnapshotCohortComponent,
        *,
        scan: CommentScanRun | None,
        now: datetime,
    ) -> bool:
        cohort = await self._lock_cohort(component.cohort_id)
        if (
            cohort.status == CohortStatus.SHADOW_PLANNED.value
            or cohort.extra.get("rollout_mode") != CohortRolloutMode.LIVE.value
        ):
            return False
        frontier_repository = FrontierStateRepository(self.session)
        frontier = await frontier_repository.get_or_create(
            target_type="video",
            target_id=cohort.bvid,
            frontier_type="latest_comments",
            now=now,
            lock=True,
        )
        scan_is_active_owner = (
            scan is not None
            and scan.status
            in {
                CommentScanStatus.PLANNED,
                CommentScanStatus.RUNNING,
                CommentScanStatus.PAUSED,
            }
            and frontier.active_scan_run_id == scan.id
        )
        changed = False
        if not scan_is_active_owner:
            plan = _latest_repair_scan_plan(
                cohort,
                component,
                frontier=frontier,
            )
            claim = await LatestScanRunRepository(self.session).claim_or_join(
                plan,
                frontier_state=frontier,
                expected_version=frontier.version,
                now=now,
            )
            scan = claim.scan
            frontier = claim.frontier_state
            if component.comment_scan_run_id != scan.id:
                component.comment_scan_run_id = scan.id
                changed = True
        if component.status != CohortComponentStatus.JOINED_ACTIVE_TASK.value:
            component.status = CohortComponentStatus.JOINED_ACTIVE_TASK.value
            component.finished_at = None
            component.failure_reason = None
            changed = True
        if scan is None:
            raise RuntimeError("Latest consumer repair did not resolve a scan")
        task_created = await self._repair_latest_scan_task(
            cohort,
            component,
            scan=scan,
            frontier=frontier,
            now=now,
        )
        if changed or task_created:
            await self._recompute_cohort(cohort, finished_at=now)
        return changed or task_created

    async def _repair_latest_scan_task(
        self,
        cohort: SnapshotCohort,
        component: SnapshotCohortComponent,
        *,
        scan: CommentScanRun,
        frontier: FrontierState,
        now: datetime,
    ) -> bool:
        if scan.status not in {
            CommentScanStatus.PLANNED,
            CommentScanStatus.PAUSED,
        }:
            return False
        slice_no = 0 if scan.status is CommentScanStatus.PLANNED else scan.slice_count
        slice_key = f"{scan.id}:{scan.mode.value}:{slice_no}"
        existing = await self.session.scalar(
            select(CollectionTask)
            .where(CollectionTask.scan_slice_key == slice_key)
            .with_for_update()
        )
        if existing is not None:
            return False
        template = await self.session.scalar(
            select(CollectionTask)
            .where(
                CollectionTask.kind == TaskKind.FETCH_LATEST_COMMENTS,
                CollectionTask.target_type == "video",
                CollectionTask.target_id == cohort.bvid,
            )
            .order_by(CollectionTask.id.desc())
            .limit(1)
        )
        repair_task = component.extra.get("repair_task")
        if not isinstance(repair_task, Mapping):
            repair_task = {}
        payload = {
            **(
                deepcopy(dict(template.payload))
                if template is not None
                else deepcopy(dict(repair_task.get("payload") or {}))
            ),
            "bvid": cohort.bvid,
            "reason": cohort.reason,
            "scheduled_for": component.scheduled_for.isoformat(),
            "cohort_key": cohort.cohort_key,
            "component_kind": component.component_kind,
            "max_scan_seconds": component.extra["max_scan_seconds"],
            "current_head_required": True,
            "scan_mode": scan.mode.value,
            "frontier_version": frontier.version,
        }
        await CollectionTaskRepository(self.session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type="video",
            target_id=cohort.bvid,
            priority=(
                template.priority
                if template is not None
                else int(repair_task.get("priority", 100))
            ),
            budget_cost=(
                template.budget_cost
                if template is not None
                else int(repair_task.get("budget_cost", 1))
            ),
            payload=payload,
            not_before=now,
            max_retries=(
                template.max_retries
                if template is not None
                else int(repair_task.get("max_retries", 3))
            ),
            idempotency_key=slice_key,
            snapshot_cohort_id=cohort.id,
            snapshot_cohort_component_id=component.id,
            comment_scan_run_id=scan.id,
            scan_slice_no=slice_no,
            scan_slice_key=slice_key,
        )
        return True

    async def _load_latest_task_scan(
        self,
        task: CollectionTask,
    ) -> CommentScanRun | None:
        if task.comment_scan_run_id is None:
            return None
        scan = await self.session.scalar(
            select(CommentScanRun)
            .where(CommentScanRun.id == task.comment_scan_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if scan is None:
            raise LookupError(f"Comment scan run not found: {task.comment_scan_run_id}")
        if scan.mode not in _LATEST_COMMENT_SCAN_MODES:
            return None
        if scan.bvid != task.target_id:
            raise ValueError("Latest task and scan reference different videos")
        return scan

    async def _effective_latest_scan(
        self,
        scan: CommentScanRun,
    ) -> CommentScanRun:
        if (
            scan.mode is not CommentScanMode.BASELINE_TAIL
            or scan.status is not CommentScanStatus.COMPLETE
            or scan.outcome != "tail_reached"
        ):
            return scan
        child = await self.session.scalar(
            select(CommentScanRun)
            .where(CommentScanRun.parent_scan_run_id == scan.id)
            .order_by(CommentScanRun.id.asc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return child or scan

    async def _latest_cumulative_counters(
        self,
        scan: CommentScanRun,
    ) -> tuple[int, int, int, int]:
        requested = scan.pages_requested
        succeeded = scan.pages_succeeded
        items = scan.items_observed
        raw_payloads = scan.raw_payloads_saved
        parent_id = scan.parent_scan_run_id
        visited = {scan.id}
        while parent_id is not None:
            if parent_id in visited:
                raise ValueError("Latest scan parent chain contains a cycle")
            visited.add(parent_id)
            parent = await self.session.scalar(
                select(CommentScanRun)
                .where(CommentScanRun.id == parent_id)
                .with_for_update()
            )
            if parent is None:
                raise LookupError(f"Comment scan run not found: {parent_id}")
            if parent.mode not in _LATEST_COMMENT_SCAN_MODES:
                raise ValueError("Latest scan parent has an incompatible mode")
            requested += parent.pages_requested
            succeeded += parent.pages_succeeded
            items += parent.items_observed
            raw_payloads += parent.raw_payloads_saved
            parent_id = parent.parent_scan_run_id
        return requested, succeeded, items, raw_payloads

    async def _terminalize_latest_task_failure(
        self,
        task: CollectionTask,
        scan: CommentScanRun,
        coverage: CollectionCoverageStat,
        *,
        finished_at: datetime,
    ) -> bool:
        frontier = await FrontierStateRepository(self.session).get_or_create(
            target_type="video",
            target_id=scan.bvid,
            frontier_type="latest_comments",
            now=finished_at,
            lock=True,
        )
        expected_version = task.payload.get("frontier_version")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise ValueError("Latest task frontier_version must be an integer")
        if expected_version < 0:
            raise ValueError("Latest task frontier_version must be non-negative")
        if frontier.active_scan_run_id not in {None, scan.id}:
            return False
        if (
            frontier.active_scan_run_id == scan.id
            and frontier.version != expected_version
        ):
            return False

        status = (
            CommentScanStatus.CORRUPTED
            if coverage.reason == "parse_error"
            else CommentScanStatus.FAILED
        )
        try:
            async with self.session.begin_nested():
                if scan.status in {
                    CommentScanStatus.PLANNED,
                    CommentScanStatus.RUNNING,
                    CommentScanStatus.PAUSED,
                }:
                    scan = await LatestScanRunRepository(self.session).mark_failed(
                        scan.id,
                        outcome="retry_exhausted",
                        error_type=str(
                            coverage.extra.get("exception_type")
                            or coverage.reason
                            or "collector_error"
                        ),
                        error_message=str(coverage.extra.get("message") or ""),
                        status=status,
                        now=finished_at,
                    )
                if frontier.active_scan_run_id == scan.id:
                    updated = await FrontierStateRepository(
                        self.session
                    ).compare_and_swap(
                        frontier.id,
                        expected_version,
                        FrontierStateUpdate(
                            frontier_rpid=frontier.frontier_rpid,
                            frontier_time=frontier.frontier_time,
                            frontier_anchor_set=frontier.frontier_anchor_set,
                            active_scan_run_id=None,
                            cursor=None,
                            last_scan_at=finished_at,
                            last_scan_status=status.value,
                            last_scan_pages=scan.pages_succeeded,
                            last_scan_truncated=True,
                            extra=deepcopy(dict(frontier.extra)),
                        ),
                        now=finished_at,
                    )
                    task.payload = {
                        **task.payload,
                        "frontier_version": updated.version,
                    }
        except FrontierVersionConflict:
            return False
        return True

    async def _load_linked(
        self,
        task: CollectionTask,
    ) -> tuple[SnapshotCohort, SnapshotCohortComponent] | None:
        return await self._load_linked_ids(
            snapshot_cohort_id=task.snapshot_cohort_id,
            snapshot_cohort_component_id=task.snapshot_cohort_component_id,
        )

    async def _load_linked_ids(
        self,
        *,
        snapshot_cohort_id: int | None,
        snapshot_cohort_component_id: int | None,
    ) -> tuple[SnapshotCohort, SnapshotCohortComponent] | None:
        if snapshot_cohort_id is None and snapshot_cohort_component_id is None:
            return None
        if snapshot_cohort_id is None or snapshot_cohort_component_id is None:
            raise ValueError("Cohort task must carry both cohort and component IDs")
        component = await self.session.scalar(
            select(SnapshotCohortComponent)
            .where(SnapshotCohortComponent.id == snapshot_cohort_component_id)
            .with_for_update()
        )
        if component is None:
            raise LookupError(
                f"Snapshot cohort component not found: {snapshot_cohort_component_id}"
            )
        cohort = await self.session.scalar(
            select(SnapshotCohort)
            .where(SnapshotCohort.id == snapshot_cohort_id)
            .with_for_update()
        )
        if cohort is None:
            raise LookupError(f"Snapshot cohort not found: {snapshot_cohort_id}")
        if component.cohort_id != cohort.id:
            raise ValueError("Task cohort/component linkage is inconsistent")
        if cohort.status == CohortStatus.SHADOW_PLANNED.value:
            raise ValueError("Shadow cohort cannot execute collection tasks")
        return cohort, component

    def _add_coverage(
        self,
        component: SnapshotCohortComponent,
        coverage: CollectionCoverageStat,
    ) -> None:
        component.requested_pages += coverage.pages_requested
        component.succeeded_pages += coverage.pages_succeeded
        component.items_observed += coverage.items_observed
        component.raw_payloads_saved += coverage.raw_payloads_saved

    async def _load_linked_scan(
        self,
        task: CollectionTask,
        *,
        cohort: SnapshotCohort,
        component: SnapshotCohortComponent,
    ) -> CommentScanRun | None:
        if task.comment_scan_run_id is None:
            return None
        if component.comment_scan_run_id != task.comment_scan_run_id:
            raise ValueError("Task and cohort component reference different scan runs")
        scan = await CommentScanRunRepository(self.session).lock(
            task.comment_scan_run_id
        )
        if scan.snapshot_cohort_id != cohort.id or scan.bvid != cohort.bvid:
            raise ValueError("Comment scan run does not belong to the task cohort")
        return scan

    @staticmethod
    def _sync_component_from_scan(
        component: SnapshotCohortComponent,
        scan: CommentScanRun,
        *,
        finished_at: datetime,
    ) -> None:
        component.requested_pages = scan.pages_requested
        component.succeeded_pages = scan.pages_succeeded
        component.items_observed = scan.items_observed
        component.raw_payloads_saved = scan.raw_payloads_saved
        component.comment_scan_run_id = scan.id
        if scan.status in {
            CommentScanStatus.PLANNED,
            CommentScanStatus.RUNNING,
            CommentScanStatus.PAUSED,
        }:
            component.status = CohortComponentStatus.RUNNING.value
            component.finished_at = None
            component.failure_reason = None
            return
        status_map = {
            CommentScanStatus.COMPLETE: CohortComponentStatus.COMPLETE,
            CommentScanStatus.PARTIAL: CohortComponentStatus.PARTIAL,
            CommentScanStatus.FAILED: CohortComponentStatus.FAILED,
            CommentScanStatus.CORRUPTED: CohortComponentStatus.CORRUPTED,
        }
        component.status = status_map[scan.status].value
        component.finished_at = scan.finished_at or finished_at
        component.failure_reason = (
            None
            if scan.status is CommentScanStatus.COMPLETE
            else scan.outcome or scan.last_error_type or scan.status.value
        )

    async def _recompute_cohort(
        self,
        cohort: SnapshotCohort,
        *,
        finished_at: datetime,
    ) -> None:
        components = list(
            await self.session.scalars(
                select(SnapshotCohortComponent)
                .where(SnapshotCohortComponent.cohort_id == cohort.id)
                .order_by(SnapshotCohortComponent.id.asc())
                .with_for_update()
            )
        )
        status = aggregate_cohort_status(
            [
                ComponentOutcome(
                    status=CohortComponentStatus(component.status),
                    required=component.required,
                    started=component.started_at is not None,
                )
                for component in components
            ]
        )
        cohort.status = status.value
        cohort.expected_component_count = len(components)
        cohort.completed_component_count = sum(
            component.status
            in {
                CohortComponentStatus.COMPLETE.value,
                CohortComponentStatus.NOT_APPLICABLE.value,
            }
            for component in components
        )
        cohort.started_at = cohort.started_at or min(
            (
                component.started_at
                for component in components
                if component.started_at is not None
            ),
            default=None,
        )
        if status in {
            CohortStatus.COMPLETE,
            CohortStatus.PARTIAL,
            CohortStatus.MISSED,
            CohortStatus.CORRUPTED,
            CohortStatus.NOT_APPLICABLE,
        }:
            cohort.finished_at = finished_at
        else:
            cohort.finished_at = None
        if status is CohortStatus.COMPLETE:
            cohort.status_reason = None
            state = await self.session.get(VideoCollectionState, cohort.bvid)
            if state is not None and (
                state.last_completed_cohort_at is None
                or finished_at > state.last_completed_cohort_at
            ):
                state.last_completed_cohort_at = finished_at
                state.updated_at = finished_at
        elif status in {CohortStatus.PARTIAL, CohortStatus.CORRUPTED}:
            cohort.status_reason = next(
                (
                    component.failure_reason
                    for component in components
                    if component.failure_reason
                ),
                cohort.status_reason,
            )
        cohort.updated_at = finished_at
        await self.session.flush()


_LATEST_COMMENT_SCAN_MODES = frozenset(
    {
        CommentScanMode.BASELINE_TAIL,
        CommentScanMode.BASELINE_HEAD_SWEEP,
        CommentScanMode.INCREMENTAL,
        CommentScanMode.FULL_RECONCILIATION,
        CommentScanMode.SEGMENTED_RECONCILIATION,
    }
)


def _latest_head_captured_at(scan: CommentScanRun) -> datetime | None:
    if scan.mode is CommentScanMode.BASELINE_TAIL:
        return None
    value = scan.extra.get("head_captured_at")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Latest scan head_captured_at must be an ISO-8601 string")
    try:
        captured_at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Latest scan head_captured_at is invalid") from exc
    _require_aware(captured_at, "head_captured_at")
    return captured_at


def _head_capture_satisfies(
    component: SnapshotCohortComponent,
    captured_at: datetime | None,
) -> bool:
    if captured_at is None or captured_at < component.scheduled_for:
        return False
    return component.deadline is None or captured_at < component.deadline


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
    immutable_extra_matches = all(
        component.extra.get(key) == value for key, value in component_plan.extra.items()
    )
    if (
        component.required != component_plan.required
        or component.scheduled_for != plan.scheduled_for
        or component.deadline != (component_plan.deadline or plan.deadline)
        or component.planned_pages != component_plan.planned_pages
        or component.extra.get("task_kind") != task_kind
        or not immutable_extra_matches
    ):
        raise ValueError(
            f"component plan conflict: {plan.cohort_key}:{component_plan.component_kind}"
        )


def _is_managed_hot_component(component_plan: CohortComponentPlan) -> bool:
    return component_plan.extra.get("scan_mode") in {"hot_core", "hot_deep"}


def _is_managed_latest_component(component_plan: CohortComponentPlan) -> bool:
    return (
        component_plan.component_kind
        in {"latest_current_head", "latest_reconciliation"}
        and component_plan.task_kind is TaskKind.FETCH_LATEST_COMMENTS
    )


def _hot_scan_run_plan(
    plan: SnapshotCohortPlan,
    *,
    cohort: SnapshotCohort,
    component_plan: CohortComponentPlan,
) -> HotScanRunPlan:
    try:
        mode = CommentScanMode(str(component_plan.extra["scan_mode"]))
        start_page = int(component_plan.extra["start_page"])
        end_page = int(component_plan.extra["end_page"])
        target_pages = int(component_plan.extra["target_pages"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Managed hot component has invalid scan settings") from exc
    if mode.value != component_plan.component_kind:
        raise ValueError("Managed hot component mode does not match component kind")
    if target_pages != component_plan.planned_pages:
        raise ValueError("Managed hot component target does not match planned pages")
    return HotScanRunPlan(
        scan_key=component_key(plan.cohort_key, component_plan.component_kind),
        bvid=plan.bvid,
        snapshot_cohort_id=cohort.id,
        mode=mode,
        target_pages=target_pages,
        start_page=start_page,
        end_page=end_page,
        policy_version=plan.policy_version,
        extra=component_plan.extra,
    )


def _latest_scan_run_plan(
    plan: SnapshotCohortPlan,
    *,
    cohort: SnapshotCohort,
    component_plan: CohortComponentPlan,
    frontier: FrontierState,
) -> LatestScanRunPlan:
    max_scan_seconds = component_plan.extra.get("max_scan_seconds")
    current_head_required = component_plan.extra.get("current_head_required")
    if (
        isinstance(max_scan_seconds, bool)
        or not isinstance(max_scan_seconds, int | float)
        or max_scan_seconds < 10
        or max_scan_seconds > 55
    ):
        raise ValueError("Managed latest component has invalid slice timing")
    if current_head_required is not True:
        raise ValueError("Managed latest component must require current-head evidence")

    baseline_status = frontier.extra.get("baseline_status")
    if baseline_status == "baseline_complete":
        mode = CommentScanMode.INCREMENTAL
    elif baseline_status == "baseline_tail_complete":
        mode = CommentScanMode.BASELINE_HEAD_SWEEP
    else:
        mode = CommentScanMode.BASELINE_TAIL
    anchors = _latest_start_anchors(frontier, mode=mode)
    start_frontier_rpid, _ = primary_anchor(anchors)
    return LatestScanRunPlan(
        scan_key=component_key(plan.cohort_key, component_plan.component_kind),
        bvid=plan.bvid,
        snapshot_cohort_id=cohort.id,
        parent_scan_run_id=None,
        mode=mode,
        policy_version=plan.policy_version,
        reason=plan.reason,
        start_frontier_rpid=start_frontier_rpid,
        start_anchor_set=anchors,
        start_cursor=frontier.cursor,
        extra=component_plan.extra,
    )


def _latest_repair_scan_plan(
    cohort: SnapshotCohort,
    component: SnapshotCohortComponent,
    *,
    frontier: FrontierState,
) -> LatestScanRunPlan:
    baseline_status = frontier.extra.get("baseline_status")
    if baseline_status == "baseline_complete":
        mode = CommentScanMode.INCREMENTAL
    elif baseline_status == "baseline_tail_complete":
        mode = CommentScanMode.BASELINE_HEAD_SWEEP
    else:
        mode = CommentScanMode.BASELINE_TAIL
    anchors = _latest_start_anchors(frontier, mode=mode)
    start_frontier_rpid, _ = primary_anchor(anchors)
    extra = {
        "max_scan_seconds": component.extra["max_scan_seconds"],
        "current_head_required": True,
    }
    return LatestScanRunPlan(
        scan_key=(
            f"{component_key(cohort.cohort_key, component.component_kind)}"
            f":continuation:{frontier.version}"
        ),
        bvid=cohort.bvid,
        snapshot_cohort_id=cohort.id,
        parent_scan_run_id=None,
        mode=mode,
        policy_version=cohort.policy_version,
        reason=cohort.reason,
        start_frontier_rpid=start_frontier_rpid,
        start_anchor_set=anchors,
        start_cursor=frontier.cursor,
        extra=extra,
    )


def _latest_start_anchors(
    frontier: FrontierState,
    *,
    mode: CommentScanMode,
) -> list[dict[str, object]]:
    if mode is CommentScanMode.BASELINE_TAIL:
        return []
    if mode is CommentScanMode.BASELINE_HEAD_SWEEP:
        legacy_rpid = frontier.extra.get("baseline_start_frontier_rpid")
        if legacy_rpid is not None:
            return list(
                normalize_anchor_set(
                    [{"rpid": legacy_rpid, "platform_created_at": None}]
                )
            )

    anchors = list(normalize_anchor_set(frontier.frontier_anchor_set))
    if not anchors and frontier.frontier_rpid is not None:
        anchors = list(
            normalize_anchor_set(
                [{"rpid": frontier.frontier_rpid, "platform_created_at": None}]
            )
        )
    return anchors


def _validate_existing_hot_task(
    task: CollectionTask,
    *,
    scan_run_id: int,
    scan_mode: CommentScanMode,
) -> None:
    expected_slice_key = f"{scan_run_id}:{scan_mode.value}:0"
    if (
        task.comment_scan_run_id != scan_run_id
        or task.scan_slice_no != 0
        or task.scan_slice_key != expected_slice_key
    ):
        raise ValueError("Existing hot task belongs to another scan slice")


def _validate_existing_latest_task(
    task: CollectionTask,
    *,
    scan_run_id: int,
    scan_mode: CommentScanMode,
) -> None:
    expected_slice_key = f"{scan_run_id}:{scan_mode.value}:0"
    if (
        task.comment_scan_run_id != scan_run_id
        or task.scan_slice_no != 0
        or task.scan_slice_key != expected_slice_key
        or task.idempotency_key != expected_slice_key
    ):
        raise ValueError("Existing latest task belongs to another scan slice")


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
