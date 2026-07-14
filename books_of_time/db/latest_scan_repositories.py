from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import (
    CollectionTask,
    CommentScanRun,
    FrontierState,
    SnapshotCohortComponent,
)
from books_of_time.db.repositories import (
    CollectionTaskRepository,
    FrontierStateRepository,
    FrontierStateUpdate,
    FrontierVersionConflict,
)
from books_of_time.domain.cohort_policy import CohortComponentStatus
from books_of_time.domain.enums import (
    CommentScanMode,
    CommentScanStatus,
    TaskKind,
)
from books_of_time.domain.latest_frontier import normalize_anchor_set, primary_anchor

_LATEST_MODES = frozenset(
    {
        CommentScanMode.BASELINE_TAIL,
        CommentScanMode.BASELINE_HEAD_SWEEP,
        CommentScanMode.INCREMENTAL,
        CommentScanMode.FULL_RECONCILIATION,
        CommentScanMode.SEGMENTED_RECONCILIATION,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        CommentScanStatus.COMPLETE,
        CommentScanStatus.PARTIAL,
        CommentScanStatus.FAILED,
        CommentScanStatus.CORRUPTED,
    }
)
_ACTIVE_STATUSES = frozenset(
    {
        CommentScanStatus.PLANNED,
        CommentScanStatus.RUNNING,
        CommentScanStatus.PAUSED,
    }
)
_UNSET = object()


@dataclass(frozen=True, slots=True)
class LatestScanRunPlan:
    scan_key: str
    bvid: str
    snapshot_cohort_id: int | None
    parent_scan_run_id: int | None
    mode: CommentScanMode
    policy_version: str
    reason: str | None
    start_frontier_rpid: int | None
    start_anchor_set: Sequence[Mapping[str, object]]
    start_cursor: str | None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LatestScanClaim:
    scan: CommentScanRun
    frontier_state: FrontierState
    created: bool


class LatestScanRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.frontiers = FrontierStateRepository(session)

    async def claim_or_join(
        self,
        plan: LatestScanRunPlan,
        *,
        frontier_state: FrontierState,
        expected_version: int,
        now: datetime,
    ) -> LatestScanClaim:
        normalized_anchors = _validate_latest_plan(plan)
        _require_aware(now, "now")
        if isinstance(expected_version, bool) or expected_version < 0:
            raise ValueError("expected_version must be non-negative")

        state = await self._lock_frontier(frontier_state.id)
        _validate_frontier_identity(state, plan)

        active_owner = await self._active_pointer(state)
        if state.version != expected_version:
            if active_owner is not None:
                _validate_join_identity(active_owner, plan, normalized_anchors)
                return LatestScanClaim(active_owner, state, False)
            raise FrontierVersionConflict(
                f"Frontier state {state.id} version changed from {expected_version}"
            )

        if active_owner is not None:
            _validate_join_identity(active_owner, plan, normalized_anchors)
            return LatestScanClaim(active_owner, state, False)
        if state.active_scan_run_id is not None:
            state = await self._replace_owner(state, None, now=now)

        active_scan = await self._find_active_by_bvid(plan.bvid, lock=True)
        if active_scan is not None:
            _validate_join_identity(active_scan, plan, normalized_anchors)
            state = await self._replace_owner(state, active_scan.id, now=now)
            return LatestScanClaim(active_scan, state, False)

        existing = await self._find_by_scan_key(plan.scan_key, lock=True)
        if existing is not None:
            _validate_scan_identity(existing, plan, normalized_anchors)
            _require_active(existing)
            state = await self._replace_owner(state, existing.id, now=now)
            return LatestScanClaim(existing, state, False)

        await self._validate_parent(plan)
        scan = _new_scan(plan, normalized_anchors, now=now)
        try:
            async with self.session.begin_nested():
                self.session.add(scan)
                await self.session.flush()
        except IntegrityError:
            winner = await self._find_active_by_bvid(plan.bvid, lock=True)
            if winner is None:
                winner = await self._find_by_scan_key(plan.scan_key, lock=True)
            if winner is None:
                raise
            if winner.scan_key == plan.scan_key:
                _validate_scan_identity(winner, plan, normalized_anchors)
            state = await self._replace_owner(state, winner.id, now=now)
            return LatestScanClaim(winner, state, False)

        state = await self._replace_owner(
            state,
            scan.id,
            cursor=plan.start_cursor,
            last_scan_status=CommentScanStatus.PLANNED.value,
            last_scan_pages=0,
            last_scan_truncated=False,
            now=now,
        )
        return LatestScanClaim(scan, state, True)

    async def complete_tail_and_create_head(
        self,
        parent_scan_run_id: int,
        *,
        frontier_state: FrontierState,
        expected_version: int,
        now: datetime,
    ) -> LatestScanClaim | None:
        _require_aware(now, "now")
        state = await self._lock_frontier(frontier_state.id)
        parent = await self.lock(parent_scan_run_id)
        if parent.mode is not CommentScanMode.BASELINE_TAIL:
            raise ValueError("Only a baseline tail can create a head sweep")
        if state.target_id != parent.bvid:
            raise ValueError("Baseline tail frontier belongs to a different BVID")

        child_key = f"{parent.scan_key}:baseline_head_sweep"
        existing_child = await self._find_by_scan_key(child_key, lock=True)
        repairing = parent.status is CommentScanStatus.COMPLETE
        if parent.status is CommentScanStatus.COMPLETE:
            if parent.outcome != "tail_reached":
                raise ValueError("Completed baseline tail has an incompatible outcome")
            if not parent.start_anchor_set:
                return None
            allowed_owners = {parent.id}
            if existing_child is not None:
                allowed_owners.add(existing_child.id)
            if state.active_scan_run_id not in allowed_owners:
                raise FrontierVersionConflict(
                    "Another latest scan owns the interrupted baseline frontier"
                )
        else:
            _require_running(parent)
        if not repairing and state.version != expected_version:
            raise FrontierVersionConflict(
                f"Frontier state {state.id} version changed from {expected_version}"
            )
        if not repairing and state.active_scan_run_id != parent.id:
            raise FrontierVersionConflict(
                f"Baseline tail {parent.id} no longer owns frontier {state.id}"
            )

        if not repairing:
            parent.status = CommentScanStatus.COMPLETE
            parent.outcome = "tail_reached"
            parent.finished_at = now
            parent.updated_at = now
            await self.session.flush()

        anchors = [
            deepcopy(item) for item in normalize_anchor_set(parent.start_anchor_set)
        ]
        if not anchors:
            extra = deepcopy(dict(state.extra))
            extra["baseline_status"] = "baseline_complete"
            extra["baseline_completed_at"] = now.isoformat()
            state = await self.frontiers.compare_and_swap(
                state.id,
                state.version,
                FrontierStateUpdate(
                    frontier_rpid=None,
                    frontier_time=None,
                    frontier_anchor_set=[],
                    active_scan_run_id=None,
                    cursor=None,
                    last_scan_at=now,
                    last_scan_status="baseline_complete",
                    last_scan_pages=parent.pages_succeeded,
                    last_scan_truncated=False,
                    extra=extra,
                ),
                now=now,
            )
            return None

        if existing_child is None:
            child = _new_scan(
                LatestScanRunPlan(
                    scan_key=child_key,
                    bvid=parent.bvid,
                    snapshot_cohort_id=parent.snapshot_cohort_id,
                    parent_scan_run_id=parent.id,
                    mode=CommentScanMode.BASELINE_HEAD_SWEEP,
                    policy_version=parent.policy_version,
                    reason=parent.reason,
                    start_frontier_rpid=parent.start_frontier_rpid,
                    start_anchor_set=anchors,
                    start_cursor="",
                    extra=parent.extra,
                ),
                anchors,
                now=now,
            )
            child.oid = parent.oid
            self.session.add(child)
            await self.session.flush()
        else:
            child = existing_child
            _validate_scan_identity(
                child,
                LatestScanRunPlan(
                    scan_key=child_key,
                    bvid=parent.bvid,
                    snapshot_cohort_id=parent.snapshot_cohort_id,
                    parent_scan_run_id=parent.id,
                    mode=CommentScanMode.BASELINE_HEAD_SWEEP,
                    policy_version=parent.policy_version,
                    reason=parent.reason,
                    start_frontier_rpid=parent.start_frontier_rpid,
                    start_anchor_set=anchors,
                    start_cursor="",
                    extra=parent.extra,
                ),
                anchors,
            )

        extra = deepcopy(dict(state.extra))
        extra["baseline_status"] = "baseline_tail_complete"
        extra["latest_scan_progress"] = {
            "scan_run_id": child.id,
            "seen_cursors": [],
        }
        if state.active_scan_run_id != child.id:
            state = await self.frontiers.compare_and_swap(
                state.id,
                state.version,
                FrontierStateUpdate(
                    frontier_rpid=state.frontier_rpid,
                    frontier_time=state.frontier_time,
                    frontier_anchor_set=state.frontier_anchor_set,
                    active_scan_run_id=child.id,
                    cursor="",
                    last_scan_at=now,
                    last_scan_status=CommentScanStatus.PLANNED.value,
                    last_scan_pages=0,
                    last_scan_truncated=False,
                    extra=extra,
                ),
                now=now,
            )

        components = list(
            await self.session.scalars(
                select(SnapshotCohortComponent)
                .where(
                    SnapshotCohortComponent.comment_scan_run_id.in_(
                        {parent.id, child.id}
                    ),
                    SnapshotCohortComponent.status.in_(
                        {
                            CohortComponentStatus.PENDING.value,
                            CohortComponentStatus.RUNNING.value,
                            CohortComponentStatus.JOINED_ACTIVE_TASK.value,
                        }
                    ),
                )
                .with_for_update()
            )
        )
        for component in components:
            component.comment_scan_run_id = child.id
            component.status = CohortComponentStatus.JOINED_ACTIVE_TASK.value

        existing_child_task = await self.session.scalar(
            select(CollectionTask)
            .where(
                CollectionTask.comment_scan_run_id == child.id,
                CollectionTask.scan_slice_no == 0,
            )
            .with_for_update()
        )
        if existing_child_task is not None:
            await self.session.flush()
            return LatestScanClaim(child, state, False)

        owner_task = await self.session.scalar(
            select(CollectionTask)
            .where(CollectionTask.comment_scan_run_id == parent.id)
            .order_by(CollectionTask.id.asc())
            .limit(1)
            .with_for_update()
        )
        owner_component = next(
            (
                component
                for component in components
                if owner_task is not None
                and component.id == owner_task.snapshot_cohort_component_id
            ),
            components[0] if components else None,
        )
        priority = owner_task.priority if owner_task is not None else 100
        budget_cost = owner_task.budget_cost if owner_task is not None else 1
        max_retries = owner_task.max_retries if owner_task is not None else 3
        payload = {
            **(deepcopy(dict(owner_task.payload)) if owner_task is not None else {}),
            "bvid": child.bvid,
            "aid": child.oid,
            "scan_mode": child.mode.value,
            "frontier_version": state.version,
            "current_head_required": True,
        }
        slice_key = f"{child.id}:{child.mode.value}:0"
        await CollectionTaskRepository(self.session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type="video",
            target_id=child.bvid,
            priority=priority,
            budget_cost=budget_cost,
            payload=payload,
            not_before=now,
            max_retries=max_retries,
            idempotency_key=slice_key,
            snapshot_cohort_id=(
                owner_task.snapshot_cohort_id if owner_task is not None else None
            ),
            snapshot_cohort_component_id=(
                owner_component.id if owner_component is not None else None
            ),
            comment_scan_run_id=child.id,
            scan_slice_no=0,
            scan_slice_key=slice_key,
        )
        await self.session.flush()
        return LatestScanClaim(child, state, existing_child is None)

    async def lock(self, scan_run_id: int) -> CommentScanRun:
        if isinstance(scan_run_id, bool) or scan_run_id <= 0:
            raise ValueError("scan_run_id must be positive")
        scan = await self.session.scalar(
            select(CommentScanRun)
            .where(CommentScanRun.id == scan_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if scan is None:
            raise LookupError(f"Comment scan run not found: {scan_run_id}")
        _require_latest(scan)
        return scan

    async def mark_running(
        self,
        scan_run_id: int,
        *,
        now: datetime,
        oid: int | None = None,
    ) -> CommentScanRun:
        _require_aware(now, "now")
        scan = await self.lock(scan_run_id)
        _require_active(scan)
        if oid is not None:
            if isinstance(oid, bool) or oid <= 0:
                raise ValueError("oid must be positive")
            if scan.oid is not None and scan.oid != oid:
                raise ValueError("Comment scan run oid cannot change")
            scan.oid = oid
        if scan.status is not CommentScanStatus.RUNNING:
            scan.slice_count += 1
        scan.status = CommentScanStatus.RUNNING
        scan.outcome = None
        scan.started_at = scan.started_at or now
        scan.finished_at = None
        scan.updated_at = now
        await self.session.flush()
        return scan

    async def record_page_requested(
        self,
        scan_run_id: int,
        *,
        now: datetime,
    ) -> CommentScanRun:
        _require_aware(now, "now")
        scan = await self.lock(scan_run_id)
        _require_running(scan)
        scan.pages_requested += 1
        scan.updated_at = now
        await self.session.flush()
        return scan

    async def record_page_succeeded(
        self,
        scan_run_id: int,
        *,
        result_cursor: str | None,
        result_anchor_set: Sequence[Mapping[str, object]],
        items_observed: int,
        raw_payloads_saved: int,
        now: datetime,
    ) -> CommentScanRun:
        _require_aware(now, "now")
        _require_non_negative(items_observed, "items_observed")
        _require_non_negative(raw_payloads_saved, "raw_payloads_saved")
        if result_cursor is not None and not isinstance(result_cursor, str):
            raise ValueError("result_cursor must be a string or null")
        anchors = [deepcopy(item) for item in normalize_anchor_set(result_anchor_set)]
        result_frontier_rpid, _ = primary_anchor(anchors)

        scan = await self.lock(scan_run_id)
        _require_running(scan)
        if scan.pages_succeeded >= scan.pages_requested:
            raise ValueError("Comment scan page success requires a recorded request")
        scan.pages_succeeded += 1
        scan.items_observed += items_observed
        scan.raw_payloads_saved += raw_payloads_saved
        scan.result_cursor = result_cursor
        scan.result_frontier_rpid = result_frontier_rpid
        scan.result_anchor_set = anchors
        scan.updated_at = now
        await self.session.flush()
        return scan

    async def mark_paused(
        self,
        scan_run_id: int,
        *,
        outcome: str,
        now: datetime,
    ) -> CommentScanRun:
        _require_aware(now, "now")
        scan = await self.lock(scan_run_id)
        _require_running(scan)
        scan.status = CommentScanStatus.PAUSED
        scan.outcome = _bounded_required_text(outcome, "outcome", 64)
        scan.finished_at = None
        scan.updated_at = now
        await self.session.flush()
        return scan

    async def mark_complete(
        self,
        scan_run_id: int,
        *,
        outcome: str,
        now: datetime,
    ) -> CommentScanRun:
        _require_aware(now, "now")
        scan = await self.lock(scan_run_id)
        _require_running(scan)
        scan.status = CommentScanStatus.COMPLETE
        scan.outcome = _bounded_required_text(outcome, "outcome", 64)
        scan.finished_at = now
        scan.updated_at = now
        await self.session.flush()
        return scan

    async def mark_failed(
        self,
        scan_run_id: int,
        *,
        outcome: str,
        now: datetime,
        error_type: str | None = None,
        error_message: str | None = None,
        status: CommentScanStatus = CommentScanStatus.FAILED,
        truncated: bool = True,
    ) -> CommentScanRun:
        _require_aware(now, "now")
        if status not in {
            CommentScanStatus.PARTIAL,
            CommentScanStatus.FAILED,
            CommentScanStatus.CORRUPTED,
        }:
            raise ValueError("Failed scan status must be partial, failed, or corrupted")
        scan = await self.lock(scan_run_id)
        _require_active(scan)
        scan.status = status
        scan.outcome = _bounded_required_text(outcome, "outcome", 64)
        scan.last_error_type = _bounded_optional_text(error_type, 120)
        scan.last_error_message = _bounded_optional_text(error_message, 2000)
        scan.truncated = truncated
        scan.finished_at = now
        scan.updated_at = now
        await self.session.flush()
        return scan

    async def _lock_frontier(self, state_id: int) -> FrontierState:
        state = await self.session.scalar(
            select(FrontierState)
            .where(FrontierState.id == state_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if state is None:
            raise LookupError(f"Frontier state not found: {state_id}")
        return state

    async def _active_pointer(self, state: FrontierState) -> CommentScanRun | None:
        if state.active_scan_run_id is None:
            return None
        scan = await self.session.scalar(
            select(CommentScanRun)
            .where(CommentScanRun.id == state.active_scan_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if scan is None:
            return None
        if scan.bvid != state.target_id:
            raise ValueError("Frontier active scan belongs to a different BVID")
        _require_latest(scan)
        if scan.status in _TERMINAL_STATUSES:
            return None
        return scan

    async def _find_active_by_bvid(
        self,
        bvid: str,
        *,
        lock: bool,
    ) -> CommentScanRun | None:
        statement = select(CommentScanRun).where(
            CommentScanRun.bvid == bvid,
            CommentScanRun.mode.in_(_LATEST_MODES),
            CommentScanRun.status.in_(_ACTIVE_STATUSES),
        )
        if lock:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        return await self.session.scalar(statement)

    async def _find_by_scan_key(
        self,
        scan_key: str,
        *,
        lock: bool,
    ) -> CommentScanRun | None:
        statement = select(CommentScanRun).where(CommentScanRun.scan_key == scan_key)
        if lock:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        return await self.session.scalar(statement)

    async def _validate_parent(self, plan: LatestScanRunPlan) -> None:
        if plan.parent_scan_run_id is None:
            return
        parent = await self.session.scalar(
            select(CommentScanRun)
            .where(CommentScanRun.id == plan.parent_scan_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if parent is None:
            raise ValueError("Latest scan parent does not exist")
        if parent.bvid != plan.bvid:
            raise ValueError("Latest scan parent belongs to a different BVID")
        _require_latest(parent)

    async def _replace_owner(
        self,
        state: FrontierState,
        active_scan_run_id: int | None,
        *,
        cursor: str | None | object = _UNSET,
        last_scan_status: str | None | object = _UNSET,
        last_scan_pages: int | object = _UNSET,
        last_scan_truncated: bool | object = _UNSET,
        now: datetime,
    ) -> FrontierState:
        return await self.frontiers.compare_and_swap(
            state.id,
            state.version,
            FrontierStateUpdate(
                frontier_rpid=state.frontier_rpid,
                frontier_time=state.frontier_time,
                frontier_anchor_set=state.frontier_anchor_set,
                active_scan_run_id=active_scan_run_id,
                cursor=state.cursor if cursor is _UNSET else cursor,
                last_scan_at=state.last_scan_at,
                last_scan_status=(
                    state.last_scan_status
                    if last_scan_status is _UNSET
                    else last_scan_status
                ),
                last_scan_pages=(
                    state.last_scan_pages
                    if last_scan_pages is _UNSET
                    else last_scan_pages
                ),
                last_scan_truncated=(
                    state.last_scan_truncated
                    if last_scan_truncated is _UNSET
                    else last_scan_truncated
                ),
                extra=state.extra,
            ),
            now=now,
        )


def _validate_latest_plan(
    plan: LatestScanRunPlan,
) -> list[dict[str, object]]:
    _required_text(plan.scan_key, "scan_key")
    _required_text(plan.bvid, "bvid")
    _required_text(plan.policy_version, "policy_version")
    if plan.mode not in _LATEST_MODES:
        raise ValueError("Latest scan mode is invalid")
    for value, name in (
        (plan.snapshot_cohort_id, "snapshot_cohort_id"),
        (plan.parent_scan_run_id, "parent_scan_run_id"),
        (plan.start_frontier_rpid, "start_frontier_rpid"),
    ):
        if value is not None and (isinstance(value, bool) or value <= 0):
            raise ValueError(f"{name} must be positive")
    if plan.start_cursor is not None and not isinstance(plan.start_cursor, str):
        raise ValueError("start_cursor must be a string or null")
    if plan.reason is not None:
        _bounded_required_text(plan.reason, "reason", 64)
    if not isinstance(plan.extra, Mapping):
        raise ValueError("extra must be a mapping")
    anchors = [deepcopy(item) for item in normalize_anchor_set(plan.start_anchor_set)]
    primary_rpid, _ = primary_anchor(anchors)
    if primary_rpid != plan.start_frontier_rpid:
        raise ValueError("start_frontier_rpid must match the primary start anchor")
    return anchors


def _validate_frontier_identity(state: FrontierState, plan: LatestScanRunPlan) -> None:
    if state.target_type != "video" or state.frontier_type != "latest_comments":
        raise ValueError("Latest scan requires a video latest_comments frontier")
    if state.target_id != plan.bvid:
        raise ValueError("Latest scan frontier belongs to a different BVID")


def _validate_scan_identity(
    scan: CommentScanRun,
    plan: LatestScanRunPlan,
    normalized_anchors: Sequence[Mapping[str, object]],
) -> None:
    stored = (
        scan.bvid,
        scan.snapshot_cohort_id,
        scan.parent_scan_run_id,
        scan.mode,
        scan.policy_version,
        scan.reason,
        scan.start_frontier_rpid,
        scan.start_anchor_set,
        scan.start_cursor,
        scan.extra,
    )
    expected = (
        plan.bvid,
        plan.snapshot_cohort_id,
        plan.parent_scan_run_id,
        plan.mode,
        plan.policy_version,
        plan.reason,
        plan.start_frontier_rpid,
        list(normalized_anchors),
        plan.start_cursor,
        dict(plan.extra),
    )
    if stored != expected:
        raise ValueError("Comment scan key has a different immutable identity")


def _validate_join_identity(
    scan: CommentScanRun,
    plan: LatestScanRunPlan,
    normalized_anchors: Sequence[Mapping[str, object]],
) -> None:
    if scan.scan_key == plan.scan_key:
        _validate_scan_identity(scan, plan, normalized_anchors)


def _new_scan(
    plan: LatestScanRunPlan,
    normalized_anchors: Sequence[Mapping[str, object]],
    *,
    now: datetime,
) -> CommentScanRun:
    return CommentScanRun(
        scan_key=plan.scan_key,
        bvid=plan.bvid,
        oid=None,
        snapshot_cohort_id=plan.snapshot_cohort_id,
        parent_scan_run_id=plan.parent_scan_run_id,
        mode=plan.mode,
        status=CommentScanStatus.PLANNED,
        outcome=None,
        started_at=None,
        finished_at=None,
        start_frontier_rpid=plan.start_frontier_rpid,
        result_frontier_rpid=None,
        start_anchor_set=[deepcopy(item) for item in normalized_anchors],
        result_anchor_set=[],
        start_cursor=plan.start_cursor,
        result_cursor=None,
        target_pages=None,
        next_page_number=None,
        pages_requested=0,
        pages_succeeded=0,
        items_observed=0,
        raw_payloads_saved=0,
        slice_count=0,
        truncated=False,
        last_error_type=None,
        last_error_message=None,
        reason=plan.reason,
        policy_version=plan.policy_version,
        extra=deepcopy(dict(plan.extra)),
        created_at=now,
        updated_at=now,
    )


def _require_latest(scan: CommentScanRun) -> None:
    if scan.mode not in _LATEST_MODES:
        raise ValueError(f"Comment scan run is not a latest scan: {scan.mode.value}")


def _require_active(scan: CommentScanRun) -> None:
    if scan.status in _TERMINAL_STATUSES:
        raise ValueError(f"Comment scan run is terminal: {scan.status.value}")


def _require_running(scan: CommentScanRun) -> None:
    _require_active(scan)
    if scan.status is not CommentScanStatus.RUNNING:
        raise ValueError(f"Comment scan run is not running: {scan.status.value}")


def _require_non_negative(value: int, name: str) -> None:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be non-negative")


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value.strip()


def _bounded_required_text(value: str, name: str, limit: int) -> str:
    normalized = _required_text(value, name)
    if len(normalized) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")
    return normalized


def _bounded_optional_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
