from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.db.models import CommentScanRun
from books_of_time.domain.enums import CommentScanMode, CommentScanStatus


@dataclass(frozen=True, slots=True)
class HotScanRunPlan:
    scan_key: str
    bvid: str
    snapshot_cohort_id: int | None
    mode: CommentScanMode
    target_pages: int
    start_page: int
    end_page: int
    policy_version: str
    extra: Mapping[str, Any] = field(default_factory=dict)


class CommentScanRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def materialize_hot(
        self,
        plan: HotScanRunPlan,
        *,
        now: datetime,
    ) -> tuple[CommentScanRun, bool]:
        _validate_hot_plan(plan)
        _require_aware(now, "now")

        existing = await self._find_by_scan_key(plan.scan_key, lock=True)
        if existing is not None:
            _validate_hot_identity(existing, plan)
            return existing, False

        extra = deepcopy(dict(plan.extra))
        extra.update({"start_page": plan.start_page, "end_page": plan.end_page})
        row = CommentScanRun(
            scan_key=plan.scan_key,
            bvid=plan.bvid,
            oid=None,
            snapshot_cohort_id=plan.snapshot_cohort_id,
            parent_scan_run_id=None,
            mode=plan.mode,
            status=CommentScanStatus.PLANNED,
            outcome=None,
            started_at=None,
            finished_at=None,
            start_frontier_rpid=None,
            result_frontier_rpid=None,
            start_anchor_set=[],
            result_anchor_set=[],
            start_cursor=None,
            result_cursor=None,
            target_pages=plan.target_pages,
            next_page_number=plan.start_page,
            pages_requested=0,
            pages_succeeded=0,
            items_observed=0,
            raw_payloads_saved=0,
            slice_count=0,
            truncated=False,
            last_error_type=None,
            last_error_message=None,
            reason=None,
            policy_version=plan.policy_version,
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
            existing = await self._find_by_scan_key(plan.scan_key, lock=True)
            if existing is None:
                raise
            _validate_hot_identity(existing, plan)
            return existing, False

    async def lock(self, scan_run_id: int) -> CommentScanRun:
        scan = await self.session.scalar(
            select(CommentScanRun)
            .where(CommentScanRun.id == scan_run_id)
            .with_for_update()
        )
        if scan is None:
            raise LookupError(f"Comment scan run not found: {scan_run_id}")
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
        page_number: int,
        now: datetime,
    ) -> CommentScanRun:
        _require_aware(now, "now")
        scan = await self.lock(scan_run_id)
        _require_running(scan)
        _validate_current_page(scan, page_number)
        scan.pages_requested += 1
        scan.updated_at = now
        await self.session.flush()
        return scan

    async def record_page_succeeded(
        self,
        scan_run_id: int,
        *,
        page_number: int,
        items_observed: int,
        raw_payloads_saved: int,
        now: datetime,
    ) -> CommentScanRun:
        _require_aware(now, "now")
        _require_non_negative(items_observed, "items_observed")
        _require_non_negative(raw_payloads_saved, "raw_payloads_saved")
        scan = await self.lock(scan_run_id)
        _require_running(scan)
        _validate_current_page(scan, page_number)
        if scan.pages_succeeded >= scan.pages_requested:
            raise ValueError("Comment scan page success requires a recorded request")
        scan.pages_succeeded += 1
        scan.items_observed += items_observed
        scan.raw_payloads_saved += raw_payloads_saved
        scan.next_page_number = page_number + 1
        scan.updated_at = now
        await self.session.flush()
        return scan

    async def record_page_failed(
        self,
        scan_run_id: int,
        *,
        page_number: int,
        error_type: str,
        error_message: str,
        now: datetime,
    ) -> CommentScanRun:
        _require_aware(now, "now")
        scan = await self.lock(scan_run_id)
        _require_running(scan)
        _validate_current_page(scan, page_number)
        scan.last_error_type = _bounded_optional_text(error_type, 120)
        scan.last_error_message = _bounded_optional_text(error_message, 2000)
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

    async def _find_by_scan_key(
        self,
        scan_key: str,
        *,
        lock: bool,
    ) -> CommentScanRun | None:
        statement = select(CommentScanRun).where(CommentScanRun.scan_key == scan_key)
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)


def _validate_hot_plan(plan: HotScanRunPlan) -> None:
    _required_text(plan.scan_key, "scan_key")
    _required_text(plan.bvid, "bvid")
    _required_text(plan.policy_version, "policy_version")
    if plan.mode not in {CommentScanMode.HOT_CORE, CommentScanMode.HOT_DEEP}:
        raise ValueError("Hot scan mode must be hot_core or hot_deep")
    if isinstance(plan.target_pages, bool) or plan.target_pages <= 0:
        raise ValueError("target_pages must be positive")
    if isinstance(plan.start_page, bool) or plan.start_page <= 0:
        raise ValueError("start_page must be positive")
    if isinstance(plan.end_page, bool) or plan.end_page < plan.start_page:
        raise ValueError("end_page must be at least start_page")
    if plan.target_pages != plan.end_page - plan.start_page + 1:
        raise ValueError("target_pages must match the configured page range")


def _validate_hot_identity(scan: CommentScanRun, plan: HotScanRunPlan) -> None:
    stored = (
        scan.bvid,
        scan.snapshot_cohort_id,
        scan.mode,
        scan.target_pages,
        scan.extra.get("start_page"),
        scan.extra.get("end_page"),
        scan.policy_version,
    )
    expected = (
        plan.bvid,
        plan.snapshot_cohort_id,
        plan.mode,
        plan.target_pages,
        plan.start_page,
        plan.end_page,
        plan.policy_version,
    )
    if stored != expected:
        raise ValueError("Comment scan key has a different immutable identity")


def _validate_current_page(scan: CommentScanRun, page_number: int) -> None:
    if isinstance(page_number, bool):
        raise ValueError("page_number must be an integer")
    start_page, end_page = _hot_bounds(scan)
    if page_number < start_page or page_number > end_page:
        raise ValueError("Comment scan page is outside configured range")
    if page_number != scan.next_page_number:
        raise ValueError(
            f"Comment scan expected page {scan.next_page_number}, got {page_number}"
        )


def _hot_bounds(scan: CommentScanRun) -> tuple[int, int]:
    start_page = scan.extra.get("start_page")
    end_page = scan.extra.get("end_page")
    if (
        not isinstance(start_page, int)
        or isinstance(start_page, bool)
        or not isinstance(end_page, int)
        or isinstance(end_page, bool)
    ):
        raise ValueError("Comment scan run is missing its immutable page range")
    return start_page, end_page


def _require_running(scan: CommentScanRun) -> None:
    _require_active(scan)
    if scan.status is not CommentScanStatus.RUNNING:
        raise ValueError(f"Comment scan run is not running: {scan.status.value}")


def _require_active(scan: CommentScanRun) -> None:
    if scan.status in {
        CommentScanStatus.COMPLETE,
        CommentScanStatus.PARTIAL,
        CommentScanStatus.FAILED,
        CommentScanStatus.CORRUPTED,
    }:
        raise ValueError(f"Comment scan run is terminal: {scan.status.value}")


def _require_non_negative(value: int, name: str) -> None:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be non-negative")


def _required_text(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


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
