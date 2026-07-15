from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.cohort_repositories import (
    CohortComponentPlan,
    CohortMaterializationResult,
    CollectionPolicyVersionRepository,
    CollectionScheduleGapRepository,
    SnapshotCohortExecutionRepository,
    SnapshotCohortPlan,
    SnapshotCohortRepository,
    VideoCollectionStateRepository,
)
from books_of_time.db.models import (
    Event,
    EventVideo,
    FrontierState,
    KnownVideo,
    KnownVideoSource,
    SnapshotCohort,
    SnapshotCohortComponent,
    VideoCollectionState,
)
from books_of_time.db.repositories import VideoMetricSnapshotRepository
from books_of_time.domain.cohort_policy import (
    CohortComponentStatus,
    CohortPolicy,
    CohortRolloutMode,
    CohortStatus,
    CollectionTier,
    ComponentOutcome,
    TierAssessment,
    TierSignals,
    VideoLifeStage,
    aggregate_cohort_status,
    apply_tier_assessment,
    checkpoint_cohort_key,
    checkpoint_times,
    component_kinds_for_stage,
    desired_tier,
    determine_life_stage,
    effective_interval,
    hot_page_plan,
    next_aligned_slot,
    recovery_cohort_key,
    routine_cohort_key,
)
from books_of_time.domain.enums import TaskKind
from books_of_time.domain.latest_frontier import latest_slice_seconds


@dataclass(frozen=True, slots=True)
class CohortPlanningSummary:
    videos_considered: int = 0
    videos_adopted: int = 0
    cohorts_created: int = 0
    components_created: int = 0
    tasks_created: int = 0
    routine_cohorts_created: int = 0
    checkpoint_cohorts_created: int = 0
    recovery_cohorts_created: int = 0
    schedule_gaps_created: int = 0


@dataclass(frozen=True, slots=True)
class _PlanningSignals:
    assessment: TierAssessment
    life_stage: VideoLifeStage
    recent_view_growth: int | None
    frontier_complete: bool


class SnapshotCohortPlanner:
    def __init__(self, policy: CohortPolicy, *, batch_limit: int = 5000) -> None:
        if batch_limit <= 0:
            raise ValueError("planner batch_limit must be positive")
        self.policy = policy
        self.batch_limit = batch_limit

    async def plan_due(
        self,
        session: AsyncSession,
        *,
        now: datetime,
        rollout_mode: CohortRolloutMode | None = None,
    ) -> CohortPlanningSummary:
        _require_aware(now)
        effective_rollout = rollout_mode or self.policy.rollout_mode
        await CollectionPolicyVersionRepository(session).ensure_configured(
            self.policy,
            now=now,
        )
        if effective_rollout is CohortRolloutMode.LIVE:
            await SnapshotCohortRepository(session).repair_latest_tail_handoffs(
                now=now,
                limit=self.batch_limit,
            )
            await SnapshotCohortExecutionRepository(session).repair_latest_consumers(
                now=now,
                limit=self.batch_limit,
            )

        state_repository = VideoCollectionStateRepository(session)
        videos = await state_repository.list_candidates(limit=self.batch_limit)
        totals = {
            "videos_considered": len(videos),
            "videos_adopted": 0,
            "cohorts_created": 0,
            "components_created": 0,
            "tasks_created": 0,
            "routine_cohorts_created": 0,
            "checkpoint_cohorts_created": 0,
            "recovery_cohorts_created": 0,
            "schedule_gaps_created": 0,
        }

        for video in videos:
            state = await state_repository.lock(video.bvid)
            first_adoption = state is None
            if state is None:
                state = await state_repository.adopt(
                    bvid=video.bvid,
                    policy_version=self.policy.policy_version,
                    adopted_at=now,
                )
                totals["videos_adopted"] += 1

            signals = await self._planning_signals(
                session,
                video=video,
                state=state,
                now=now,
            )
            video_totals, next_due_at, last_checkpoint_hours = await self._plan_video(
                session,
                video=video,
                state=state,
                signals=signals,
                now=now,
                rollout_mode=effective_rollout,
                first_active_adoption=first_adoption,
            )
            for key, value in video_totals.items():
                totals[key] += value
            await state_repository.record_planning(
                bvid=video.bvid,
                assessment=signals.assessment,
                life_stage=signals.life_stage,
                policy_version=self.policy.policy_version,
                next_due_at=next_due_at,
                last_planned_at=now,
                last_checkpoint_hours=last_checkpoint_hours,
                updated_at=now,
            )

        await session.flush()
        return CohortPlanningSummary(**totals)

    async def _planning_signals(
        self,
        session: AsyncSession,
        *,
        video: KnownVideo,
        state: VideoCollectionState,
        now: datetime,
    ) -> _PlanningSignals:
        monitored_official = bool(
            await session.scalar(
                select(func.count(KnownVideoSource.id)).where(
                    KnownVideoSource.bvid == video.bvid,
                    KnownVideoSource.active.is_(True),
                    KnownVideoSource.monitored.is_(True),
                    KnownVideoSource.official.is_(True),
                )
            )
        )
        active_event = (
            await session.scalar(
                select(EventVideo.bvid)
                .join(Event, Event.id == EventVideo.event_id)
                .where(
                    EventVideo.bvid == video.bvid,
                    EventVideo.active.is_(True),
                    Event.status == "active",
                )
                .limit(1)
            )
            is not None
        )
        view_growth = await VideoMetricSnapshotRepository(
            session
        ).get_view_growth_since(
            bvid=video.bvid,
            since=now - timedelta(hours=1),
            now=now,
        )
        publish_age = max(now - state.schedule_anchor_at, timedelta())
        pinned_tier = (
            CollectionTier(state.pinned_tier) if state.pinned_tier is not None else None
        )
        desired = desired_tier(
            TierSignals(
                monitored_official=monitored_official,
                publish_age=publish_age,
                active_event_core=active_event,
                pinned_tier=pinned_tier,
                view_growth_per_hour=view_growth,
            ),
            self.policy,
        )
        assessment = apply_tier_assessment(
            current_effective=CollectionTier(state.effective_tier),
            desired=desired,
            candidate_downgrade=(
                CollectionTier(state.candidate_downgrade_tier)
                if state.candidate_downgrade_tier is not None
                else None
            ),
            consecutive_count=state.consecutive_downgrade_count,
            policy=self.policy,
        )
        b_threshold = self.policy.tier_thresholds[CollectionTier.B]
        low_growth_evidence = (
            view_growth is not None and view_growth < b_threshold.view_growth_per_hour
        )
        renewed_growth = (
            view_growth is not None and view_growth >= b_threshold.view_growth_per_hour
        )
        life_stage = determine_life_stage(
            publish_age,
            low_growth_evidence=low_growth_evidence,
            policy=self.policy,
            active_event=active_event,
            operator_pinned=state.pinned_tier is not None,
            renewed_growth=renewed_growth,
        )
        frontier = await session.scalar(
            select(FrontierState).where(
                FrontierState.target_type == "video",
                FrontierState.target_id == video.bvid,
                FrontierState.frontier_type == "latest_comments",
            )
        )
        frontier_complete = bool(
            frontier is not None
            and frontier.extra.get("baseline_status") == "baseline_complete"
        )
        return _PlanningSignals(
            assessment=assessment,
            life_stage=life_stage,
            recent_view_growth=view_growth,
            frontier_complete=frontier_complete,
        )

    async def _plan_video(
        self,
        session: AsyncSession,
        *,
        video: KnownVideo,
        state: VideoCollectionState,
        signals: _PlanningSignals,
        now: datetime,
        rollout_mode: CohortRolloutMode,
        first_active_adoption: bool,
    ) -> tuple[dict[str, int], datetime | None, int | None]:
        totals = {
            "cohorts_created": 0,
            "components_created": 0,
            "tasks_created": 0,
            "routine_cohorts_created": 0,
            "checkpoint_cohorts_created": 0,
            "recovery_cohorts_created": 0,
            "schedule_gaps_created": 0,
        }
        materializer = SnapshotCohortRepository(session)
        current_bucket = _floor_bucket(now, self.policy.planning_seconds)
        checkpoint_rows = checkpoint_times(state.schedule_anchor_at, self.policy)
        next_checkpoint_at = next(
            (scheduled for _hours, scheduled in checkpoint_rows if scheduled > now),
            None,
        )
        interval = _routine_interval(
            state.schedule_anchor_at,
            now,
            signals=signals,
            policy=self.policy,
            next_checkpoint_at=next_checkpoint_at,
        )
        overdue_component_plans: dict[str, CohortComponentPlan] = {}
        latest_overdue_hours: int | None = None
        last_checkpoint_hours = state.last_checkpoint_hours
        coalesced_routine = False

        for checkpoint_hours, scheduled_for in checkpoint_rows:
            if scheduled_for > now:
                continue
            last_checkpoint_hours = max(last_checkpoint_hours or 0, checkpoint_hours)
            deadline = scheduled_for + self.policy.checkpoint_max_lateness
            existing = await session.scalar(
                select(SnapshotCohort).where(
                    SnapshotCohort.cohort_key
                    == checkpoint_cohort_key(video.bvid, checkpoint_hours)
                )
            )
            checkpoint_needs_recovery = False
            if (
                existing is not None
                and existing.status == CohortStatus.SHADOW_PLANNED.value
            ):
                target_status = CohortStatus(
                    existing.extra.get(
                        "shadow_target_status",
                        CohortStatus.PLANNED.value,
                    )
                )
                status_reason = existing.status_reason
                component_status = {
                    CohortStatus.PLANNED: CohortComponentStatus.PENDING,
                    CohortStatus.MISSED: (
                        CohortComponentStatus.MISSED_DUE_TO_SERVICE_GAP
                    ),
                    CohortStatus.NOT_APPLICABLE: (CohortComponentStatus.NOT_APPLICABLE),
                }.get(target_status, CohortComponentStatus.PENDING)
                checkpoint_needs_recovery = target_status is CohortStatus.MISSED
            elif video.first_seen_at > scheduled_for:
                target_status = CohortStatus.NOT_APPLICABLE
                status_reason = "not_applicable_before_discovery"
                component_status = CohortComponentStatus.NOT_APPLICABLE
            elif now <= deadline:
                target_status = CohortStatus.PLANNED
                status_reason = None
                component_status = CohortComponentStatus.PENDING
            else:
                if existing is None:
                    target_status = CohortStatus.MISSED
                    status_reason = "missed_due_to_service_gap"
                    component_status = CohortComponentStatus.MISSED_DUE_TO_SERVICE_GAP
                else:
                    await _finalize_expired_checkpoint(session, existing, now=now)
                    target_status = CohortStatus(existing.status)
                    status_reason = existing.status_reason
                    component_status = CohortComponentStatus.PENDING
                checkpoint_needs_recovery = True

            in_current_bucket = (
                _floor_bucket(scheduled_for, self.policy.planning_seconds)
                == current_bucket
                and target_status is CohortStatus.PLANNED
            )
            coalesced_routine = coalesced_routine or in_current_bucket
            component_kinds = (
                "video_metrics",
                "hot_core",
                "latest_reconciliation",
            )
            checkpoint_desired_tier = (
                CollectionTier(existing.desired_tier)
                if existing is not None
                else signals.assessment.desired
            )
            checkpoint_effective_tier = (
                CollectionTier(existing.effective_tier)
                if existing is not None
                else signals.assessment.effective
            )
            checkpoint_component_plans = _component_plans_for_kinds(
                component_kinds,
                policy=self.policy,
                tier=checkpoint_effective_tier,
                include_hot_deep=True,
                dormant=False,
                status=component_status,
                priority_for=_checkpoint_priority,
                latest_interval_seconds=None,
            )
            plan = SnapshotCohortPlan(
                cohort_key=checkpoint_cohort_key(video.bvid, checkpoint_hours),
                bvid=video.bvid,
                scheduled_for=scheduled_for,
                reason="age_checkpoint",
                age_checkpoint_hours=checkpoint_hours,
                desired_tier=checkpoint_desired_tier,
                effective_tier=checkpoint_effective_tier,
                policy_version=self.policy.policy_version,
                deadline=deadline,
                status=target_status,
                status_reason=status_reason,
                extra={
                    "checkpoint_hours": checkpoint_hours,
                    "coalesced_routine_bucket": in_current_bucket,
                },
                components=checkpoint_component_plans,
            )
            result = await materializer.materialize(
                plan,
                rollout_mode=rollout_mode,
                now=now,
            )
            _accumulate_result(totals, result)
            totals["checkpoint_cohorts_created"] += int(result.cohort_created)

            if checkpoint_needs_recovery:
                latest_overdue_hours = max(
                    latest_overdue_hours or 0,
                    checkpoint_hours,
                )
                missing_plans = _missing_component_plans(
                    result.components,
                    checkpoint_component_plans,
                )
                for missing_plan in missing_plans:
                    recovery_candidate = replace(
                        missing_plan,
                        status=CohortComponentStatus.PENDING,
                        priority=_recovery_priority(missing_plan.component_kind),
                    )
                    existing_candidate = overdue_component_plans.get(
                        missing_plan.component_kind
                    )
                    overdue_component_plans[missing_plan.component_kind] = (
                        recovery_candidate
                        if existing_candidate is None
                        else _prefer_recovery_component_plan(
                            existing_candidate,
                            recovery_candidate,
                        )
                    )

        if overdue_component_plans and latest_overdue_hours is not None:
            recovery_key = recovery_cohort_key(video.bvid, latest_overdue_hours)
            existing_recovery = await session.scalar(
                select(SnapshotCohort).where(SnapshotCohort.cohort_key == recovery_key)
            )
            if existing_recovery is None:
                recovery_scheduled_for = current_bucket
                recovery_deadline = _next_due(
                    state.schedule_anchor_at,
                    current_bucket,
                    interval,
                    signals.life_stage,
                    now=now,
                )
                recovery_desired = signals.assessment.desired
                recovery_effective = signals.assessment.effective
            else:
                recovery_scheduled_for = existing_recovery.scheduled_for
                recovery_deadline = existing_recovery.deadline
                recovery_desired = CollectionTier(existing_recovery.desired_tier)
                recovery_effective = CollectionTier(existing_recovery.effective_tier)
            recovery_coalesces_routine = recovery_scheduled_for == current_bucket
            recovery_plan = SnapshotCohortPlan(
                cohort_key=recovery_key,
                bvid=video.bvid,
                scheduled_for=recovery_scheduled_for,
                reason="recovery",
                age_checkpoint_hours=None,
                desired_tier=recovery_desired,
                effective_tier=recovery_effective,
                policy_version=self.policy.policy_version,
                deadline=recovery_deadline,
                status=CohortStatus.PLANNED,
                status_reason="checkpoint_recovery",
                extra={
                    "latest_overdue_hours": latest_overdue_hours,
                    "coalesced_routine_bucket": recovery_coalesces_routine,
                },
                components=tuple(
                    overdue_component_plans[kind]
                    for kind in _ordered_component_kinds(set(overdue_component_plans))
                ),
            )
            result = await materializer.materialize(
                recovery_plan,
                rollout_mode=rollout_mode,
                now=now,
            )
            _accumulate_result(totals, result)
            totals["recovery_cohorts_created"] += int(result.cohort_created)
            coalesced_routine = coalesced_routine or recovery_coalesces_routine

        routine_due = state.next_due_at is None or state.next_due_at <= now
        next_due_at = state.next_due_at
        if routine_due:
            next_due_at = _next_due(
                state.schedule_anchor_at,
                current_bucket,
                interval,
                signals.life_stage,
                now=now,
            )
            if state.next_due_at is not None and state.next_due_at < current_bucket:
                missed_count = int((current_bucket - state.next_due_at) // interval)
                if missed_count > 0:
                    gap_end = state.next_due_at + interval * missed_count
                    _gap, created = await CollectionScheduleGapRepository(
                        session
                    ).record(
                        bvid=video.bvid,
                        gap_start=state.next_due_at,
                        gap_end=gap_end,
                        expected_cohort_count=missed_count,
                        reason="service_offline",
                        policy_version=self.policy.policy_version,
                        created_at=now,
                    )
                    totals["schedule_gaps_created"] += int(created)

            if not coalesced_routine:
                component_kinds = component_kinds_for_stage(
                    signals.life_stage,
                    frontier_complete=signals.frontier_complete,
                )
                routine_plan = SnapshotCohortPlan(
                    cohort_key=routine_cohort_key(video.bvid, current_bucket),
                    bvid=video.bvid,
                    scheduled_for=current_bucket,
                    reason="routine",
                    age_checkpoint_hours=None,
                    desired_tier=signals.assessment.desired,
                    effective_tier=signals.assessment.effective,
                    policy_version=self.policy.policy_version,
                    deadline=next_due_at,
                    status=CohortStatus.PLANNED,
                    status_reason=None,
                    extra={"planner_bucket_seconds": self.policy.planning_seconds},
                    components=_component_plans_for_kinds(
                        component_kinds,
                        policy=self.policy,
                        tier=signals.assessment.effective,
                        include_hot_deep=(
                            first_active_adoption
                            and signals.life_stage is VideoLifeStage.ACTIVE
                        ),
                        dormant=signals.life_stage is VideoLifeStage.DORMANT,
                        status=CohortComponentStatus.PENDING,
                        priority_for=lambda kind: _routine_priority(
                            signals.assessment.effective,
                            kind,
                        ),
                        latest_interval_seconds=interval.total_seconds(),
                    ),
                )
                result = await materializer.materialize(
                    routine_plan,
                    rollout_mode=rollout_mode,
                    now=now,
                )
                _accumulate_result(totals, result)
                totals["routine_cohorts_created"] += int(result.cohort_created)

        return totals, next_due_at, last_checkpoint_hours


def _component_plan(
    component_kind: str,
    *,
    status: CohortComponentStatus,
    priority: int,
    latest_interval_seconds: float | int | None = None,
) -> CohortComponentPlan:
    task_kind = {
        "video_metrics": TaskKind.FETCH_VIDEO_STATS,
        "hot_core": TaskKind.FETCH_HOT_COMMENTS,
        "hot_deep": TaskKind.FETCH_HOT_COMMENTS,
        "latest_current_head": TaskKind.FETCH_LATEST_COMMENTS,
        "latest_reconciliation": TaskKind.FETCH_LATEST_COMMENTS,
    }[component_kind]
    payload: dict[str, object] = {}
    extra: dict[str, object] = {}
    if component_kind == "hot_core":
        payload = {"page": 1, "page_limit": 1}
    elif component_kind in {"latest_current_head", "latest_reconciliation"}:
        extra = {
            "max_scan_seconds": latest_slice_seconds(latest_interval_seconds),
            "current_head_required": True,
        }
        payload = dict(extra)
    return CohortComponentPlan(
        component_kind=component_kind,
        task_kind=task_kind,
        planned_pages=1,
        status=status,
        priority=priority,
        payload=payload,
        extra=extra,
    )


def _component_plans_for_kinds(
    component_kinds: tuple[str, ...],
    *,
    policy: CohortPolicy,
    tier: CollectionTier,
    include_hot_deep: bool,
    dormant: bool,
    status: CohortComponentStatus,
    priority_for: Callable[[str], int],
    latest_interval_seconds: float | int | None = None,
) -> tuple[CohortComponentPlan, ...]:
    plans: list[CohortComponentPlan] = []
    for kind in component_kinds:
        if kind == "hot_core":
            plans.extend(
                _hot_component_plans(
                    policy,
                    tier,
                    include_deep=include_hot_deep,
                    dormant=dormant,
                    status=status,
                    priority_for=priority_for,
                )
            )
            continue
        plans.append(
            _component_plan(
                kind,
                status=status,
                priority=priority_for(kind),
                latest_interval_seconds=latest_interval_seconds,
            )
        )
    return tuple(plans)


def _hot_component_plans(
    policy: CohortPolicy,
    tier: CollectionTier,
    *,
    include_deep: bool,
    dormant: bool,
    status: CohortComponentStatus,
    priority_for: Callable[[str], int],
) -> tuple[CohortComponentPlan, ...]:
    page_plan = hot_page_plan(
        policy,
        tier,
        include_deep=include_deep,
        dormant=dormant,
    )
    ranges = [
        ("hot_core", page_plan.core_start_page, page_plan.core_pages),
    ]
    if page_plan.deep_pages > 0:
        ranges.append(("hot_deep", page_plan.deep_start_page, page_plan.deep_pages))
    return tuple(
        _hot_component_plan(
            kind,
            start_page=start_page,
            target_pages=target_pages,
            policy=policy,
            status=status,
            priority=priority_for(kind),
        )
        for kind, start_page, target_pages in ranges
    )


def _hot_component_plan(
    component_kind: str,
    *,
    start_page: int,
    target_pages: int,
    policy: CohortPolicy,
    status: CohortComponentStatus,
    priority: int,
) -> CohortComponentPlan:
    end_page = start_page + target_pages - 1
    scan_settings = {
        "scan_mode": component_kind,
        "start_page": start_page,
        "end_page": end_page,
        "target_pages": target_pages,
        "max_pages_per_slice": policy.hot_comments.max_pages_per_slice,
        "max_scan_seconds": policy.hot_comments.max_slice_seconds,
    }
    return CohortComponentPlan(
        component_kind=component_kind,
        task_kind=TaskKind.FETCH_HOT_COMMENTS,
        planned_pages=target_pages,
        status=status,
        priority=priority,
        payload={
            **scan_settings,
            "page": start_page,
            "page_limit": target_pages,
        },
        extra=scan_settings,
    )


def _routine_interval(
    anchor: datetime,
    now: datetime,
    *,
    signals: _PlanningSignals,
    policy: CohortPolicy,
    next_checkpoint_at: datetime | None,
) -> timedelta:
    if signals.life_stage is VideoLifeStage.DORMANT:
        return policy.lifecycle.dormant_interval
    if signals.life_stage is VideoLifeStage.ARCHIVED:
        return policy.lifecycle.archived_metric_probe_interval
    return effective_interval(
        anchor,
        now,
        tier=signals.assessment.effective,
        policy=policy,
        recent_view_growth_last_hour=signals.recent_view_growth,
        next_checkpoint_at=next_checkpoint_at,
    )


def _next_due(
    anchor: datetime,
    current_bucket: datetime,
    interval: timedelta,
    life_stage: VideoLifeStage,
    *,
    now: datetime,
) -> datetime:
    if life_stage is VideoLifeStage.ACTIVE:
        return next_aligned_slot(anchor, max(current_bucket, now), interval)
    return current_bucket + interval


def _missing_component_plans(
    components: tuple[SnapshotCohortComponent, ...],
    default_plans: tuple[CohortComponentPlan, ...],
) -> tuple[CohortComponentPlan, ...]:
    plans_by_kind = {plan.component_kind: plan for plan in default_plans}
    if not components:
        return default_plans
    missing: list[CohortComponentPlan] = []
    for component in components:
        if component.status in {
            CohortComponentStatus.COMPLETE.value,
            CohortComponentStatus.NOT_APPLICABLE.value,
        }:
            continue
        fallback = plans_by_kind[component.component_kind]
        if component.component_kind in {"hot_core", "hot_deep"}:
            scan_keys = (
                "scan_mode",
                "start_page",
                "end_page",
                "target_pages",
                "max_pages_per_slice",
                "max_scan_seconds",
            )
            scan_settings = {
                key: component.extra[key] for key in scan_keys if key in component.extra
            }
            if len(scan_settings) == len(scan_keys):
                fallback = replace(
                    fallback,
                    planned_pages=component.planned_pages,
                    payload={
                        **scan_settings,
                        "page": scan_settings["start_page"],
                        "page_limit": component.planned_pages,
                    },
                    extra=scan_settings,
                )
        else:
            fallback = replace(fallback, planned_pages=component.planned_pages)
        missing.append(fallback)
    return tuple(missing)


def _prefer_recovery_component_plan(
    current: CohortComponentPlan,
    candidate: CohortComponentPlan,
) -> CohortComponentPlan:
    if current.component_kind != candidate.component_kind:
        raise ValueError("recovery component kinds must match")
    if current.component_kind not in {"hot_core", "hot_deep"}:
        return current

    def range_rank(plan: CohortComponentPlan) -> tuple[int, int, int]:
        return (
            int(plan.extra["end_page"]),
            plan.planned_pages,
            -int(plan.extra["start_page"]),
        )

    return candidate if range_rank(candidate) > range_rank(current) else current


async def _finalize_expired_checkpoint(
    session: AsyncSession,
    cohort: SnapshotCohort,
    *,
    now: datetime,
) -> None:
    if cohort.deadline is None or now <= cohort.deadline:
        return
    components = list(
        await session.scalars(
            select(SnapshotCohortComponent)
            .where(SnapshotCohortComponent.cohort_id == cohort.id)
            .with_for_update()
        )
    )
    capacity_miss = False
    service_miss = False
    for component in components:
        if component.status == CohortComponentStatus.PENDING.value:
            component.status = CohortComponentStatus.MISSED_DUE_TO_CAPACITY.value
            component.finished_at = now
            component.failure_reason = "missed_due_to_capacity"
            capacity_miss = True
        elif component.status == CohortComponentStatus.BLOCKED.value:
            component.status = CohortComponentStatus.MISSED_DUE_TO_SERVICE_GAP.value
            component.finished_at = now
            component.failure_reason = "missed_due_to_service_gap"
            service_miss = True
    if components:
        cohort.status = aggregate_cohort_status(
            [_component_outcome(component) for component in components]
        ).value
        if service_miss:
            cohort.status_reason = "missed_due_to_service_gap"
        elif capacity_miss:
            cohort.status_reason = "missed_due_to_capacity"
        cohort.finished_at = now
        cohort.updated_at = now
    await session.flush()


def _component_outcome(component: SnapshotCohortComponent) -> ComponentOutcome:
    return ComponentOutcome(
        status=CohortComponentStatus(component.status),
        required=component.required,
        started=component.started_at is not None,
    )


def _ordered_component_kinds(component_kinds: set[str]) -> tuple[str, ...]:
    order = (
        "video_metrics",
        "hot_core",
        "hot_deep",
        "latest_current_head",
        "latest_reconciliation",
    )
    return tuple(kind for kind in order if kind in component_kinds)


def _routine_priority(tier: CollectionTier, component_kind: str) -> int:
    base = {
        CollectionTier.S: 100,
        CollectionTier.A: 90,
        CollectionTier.B: 80,
        CollectionTier.C: 70,
    }[tier]
    return base + _component_priority_offset(component_kind)


def _checkpoint_priority(component_kind: str) -> int:
    return 120 + _component_priority_offset(component_kind)


def _recovery_priority(component_kind: str) -> int:
    return 115 + _component_priority_offset(component_kind)


def _component_priority_offset(component_kind: str) -> int:
    return {
        "video_metrics": 2,
        "hot_core": 1,
        "hot_deep": 0,
        "latest_current_head": 0,
        "latest_reconciliation": 0,
    }[component_kind]


def _accumulate_result(
    totals: dict[str, int],
    result: CohortMaterializationResult,
) -> None:
    totals["cohorts_created"] += int(result.cohort_created)
    totals["components_created"] += result.components_created
    totals["tasks_created"] += result.tasks_created


def _floor_bucket(value: datetime, seconds: int) -> datetime:
    _require_aware(value)
    timestamp = int(value.astimezone(UTC).timestamp())
    bucket = timestamp - (timestamp % seconds)
    return datetime.fromtimestamp(bucket, tz=UTC)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("planner now must be timezone-aware")
