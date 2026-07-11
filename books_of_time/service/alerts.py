from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from books_of_time.common.logger import get_logger
from books_of_time.db.models import (
    CollectionCoverageStat,
    CollectionTask,
    OperationalAlertState,
    ScheduledJob,
)
from books_of_time.db.repositories import ServiceInstanceRepository
from books_of_time.domain.enums import TaskStatus

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OperationalAlertPolicy:
    worker_heartbeat_timeout_seconds: int = 90
    pending_task_threshold: int = 1000
    oldest_pending_seconds: int = 900
    request_failure_window_seconds: int = 3600
    request_failure_min_pages: int = 20
    request_failure_rate: float = 0.25
    scheduled_job_failure_threshold: int = 3
    repeat_notification_seconds: int = 3600

    def __post_init__(self) -> None:
        integer_thresholds = {
            "worker_heartbeat_timeout_seconds": self.worker_heartbeat_timeout_seconds,
            "pending_task_threshold": self.pending_task_threshold,
            "oldest_pending_seconds": self.oldest_pending_seconds,
            "request_failure_window_seconds": self.request_failure_window_seconds,
            "request_failure_min_pages": self.request_failure_min_pages,
            "scheduled_job_failure_threshold": self.scheduled_job_failure_threshold,
            "repeat_notification_seconds": self.repeat_notification_seconds,
        }
        for name, value in integer_thresholds.items():
            if value < 1:
                raise ValueError(f"Operational alert {name} threshold must be positive")
        if not 0 < self.request_failure_rate <= 1:
            raise ValueError(
                "Operational alert request_failure_rate must be greater than 0 "
                "and at most 1"
            )

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> OperationalAlertPolicy:
        values = config or {}
        if not isinstance(values, dict):
            raise ValueError("operations.alerts configuration must be a mapping")
        supported = set(cls.__dataclass_fields__)
        unknown = set(values) - supported - {"enabled", "evaluation_seconds"}
        if unknown:
            raise ValueError(
                "Unsupported operations.alerts keys: " + ", ".join(sorted(unknown))
            )
        return cls(**{name: values[name] for name in supported if name in values})


@dataclass(frozen=True, slots=True)
class AlertTransition:
    action: str
    state: OperationalAlertState


class AlertNotifier(Protocol):
    async def notify(self, transition: AlertTransition) -> None: ...


class LogAlertNotifier:
    async def notify(self, transition: AlertTransition) -> None:
        state = transition.state
        log = logger.info if transition.action == "resolved" else logger.warning
        log(
            "Operational alert action=%s key=%s type=%s severity=%s summary=%s details=%s",
            transition.action,
            state.alert_key,
            state.alert_type,
            state.severity,
            state.summary,
            state.details,
        )


@dataclass(frozen=True, slots=True)
class AlertEvaluationSummary:
    evaluated_count: int
    triggered_count: int
    resolved_count: int
    notification_count: int


@dataclass(frozen=True, slots=True)
class _AlertDraft:
    alert_key: str
    alert_type: str
    severity: str
    triggered: bool
    summary: str
    details: dict[str, Any]


class OperationalAlertEvaluator:
    def __init__(
        self,
        *,
        policy: OperationalAlertPolicy | None = None,
        notifier: AlertNotifier | None = None,
    ) -> None:
        self.policy = policy or OperationalAlertPolicy()
        self.notifier = notifier or LogAlertNotifier()

    async def evaluate(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> AlertEvaluationSummary:
        drafts = [
            await self._request_failure_draft(session, now=now),
            *await self._scheduled_job_drafts(session),
            await self._task_backlog_draft(session, now=now),
            await self._worker_heartbeat_draft(session, now=now),
        ]
        transitions: list[AlertTransition] = []
        for draft in drafts:
            transition = await self._apply_draft(session, draft=draft, now=now)
            if transition is None:
                continue
            await self.notifier.notify(transition)
            transition.state.last_notified_at = now
            transition.state.updated_at = now
            transitions.append(transition)
        await session.flush()
        return AlertEvaluationSummary(
            evaluated_count=len(drafts),
            triggered_count=sum(draft.triggered for draft in drafts),
            resolved_count=sum(
                transition.action == "resolved" for transition in transitions
            ),
            notification_count=len(transitions),
        )

    async def _worker_heartbeat_draft(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> _AlertDraft:
        fresh = await ServiceInstanceRepository(session).has_fresh_running_instance(
            now=now,
            timeout_seconds=self.policy.worker_heartbeat_timeout_seconds,
            role="worker",
        )
        return _AlertDraft(
            alert_key="worker_heartbeat",
            alert_type="worker_heartbeat",
            severity="critical",
            triggered=not fresh,
            summary="No fresh running worker heartbeat",
            details={
                "fresh_worker_found": fresh,
                "timeout_seconds": self.policy.worker_heartbeat_timeout_seconds,
            },
        )

    async def _task_backlog_draft(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> _AlertDraft:
        pending_count, oldest_pending = (
            await session.execute(
                select(
                    func.count(CollectionTask.id),
                    func.min(CollectionTask.created_at),
                ).where(CollectionTask.status == TaskStatus.PENDING)
            )
        ).one()
        count = int(pending_count or 0)
        oldest_pending = _as_utc(oldest_pending)
        age_seconds = (
            max(int((now - oldest_pending).total_seconds()), 0)
            if oldest_pending is not None
            else 0
        )
        triggered = (
            count >= self.policy.pending_task_threshold
            or age_seconds >= self.policy.oldest_pending_seconds
        )
        return _AlertDraft(
            alert_key="task_backlog",
            alert_type="task_backlog",
            severity="warning",
            triggered=triggered,
            summary="Collection task backlog exceeded its threshold",
            details={
                "pending_count": count,
                "pending_task_threshold": self.policy.pending_task_threshold,
                "oldest_pending_age_seconds": age_seconds,
                "oldest_pending_seconds": self.policy.oldest_pending_seconds,
            },
        )

    async def _request_failure_draft(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> _AlertDraft:
        since = now - timedelta(seconds=self.policy.request_failure_window_seconds)
        pages, errors = (
            await session.execute(
                select(
                    func.sum(CollectionCoverageStat.pages_requested),
                    func.sum(CollectionCoverageStat.request_errors),
                ).where(
                    CollectionCoverageStat.finished_at >= since,
                    CollectionCoverageStat.finished_at <= now,
                )
            )
        ).one()
        page_count = int(pages or 0)
        error_count = int(errors or 0)
        rate = error_count / page_count if page_count else None
        triggered = (
            page_count >= self.policy.request_failure_min_pages
            and rate is not None
            and rate >= self.policy.request_failure_rate
        )
        return _AlertDraft(
            alert_key="request_failure_rate",
            alert_type="request_failure_rate",
            severity="critical",
            triggered=triggered,
            summary="Request failure rate exceeded its threshold",
            details={
                "since": since.isoformat(),
                "until": now.isoformat(),
                "pages_requested": page_count,
                "request_errors": error_count,
                "failure_rate": rate,
                "minimum_pages": self.policy.request_failure_min_pages,
                "failure_rate_threshold": self.policy.request_failure_rate,
            },
        )

    async def _scheduled_job_drafts(
        self,
        session: AsyncSession,
    ) -> list[_AlertDraft]:
        jobs = list(
            await session.scalars(
                select(ScheduledJob)
                .where(ScheduledJob.enabled.is_(True))
                .order_by(ScheduledJob.job_key.asc())
            )
        )
        drafts = [
            _AlertDraft(
                alert_key=f"scheduled_job_failure:{job.job_key}",
                alert_type="scheduled_job_failure",
                severity="critical",
                triggered=(
                    job.consecutive_failures
                    >= self.policy.scheduled_job_failure_threshold
                ),
                summary=f"Scheduled job {job.job_key} repeatedly failed",
                details={
                    "job_key": job.job_key,
                    "job_kind": job.job_kind.value,
                    "consecutive_failures": job.consecutive_failures,
                    "failure_threshold": (self.policy.scheduled_job_failure_threshold),
                    "last_failed_at": (
                        job.last_failed_at.isoformat()
                        if job.last_failed_at is not None
                        else None
                    ),
                    "last_error_type": job.last_error_type,
                },
            )
            for job in jobs
        ]
        known_keys = {draft.alert_key for draft in drafts}
        stale_alerts = list(
            await session.scalars(
                select(OperationalAlertState).where(
                    OperationalAlertState.alert_type == "scheduled_job_failure",
                    OperationalAlertState.status == "active",
                )
            )
        )
        for state in stale_alerts:
            if state.alert_key in known_keys:
                continue
            drafts.append(
                _AlertDraft(
                    alert_key=state.alert_key,
                    alert_type=state.alert_type,
                    severity=state.severity,
                    triggered=False,
                    summary=state.summary,
                    details={"scheduled_job_present": False},
                )
            )
        return sorted(drafts, key=lambda draft: draft.alert_key)

    async def _apply_draft(
        self,
        session: AsyncSession,
        *,
        draft: _AlertDraft,
        now: datetime,
    ) -> AlertTransition | None:
        state = await session.get(OperationalAlertState, draft.alert_key)
        if state is None:
            if not draft.triggered:
                return None
            state = OperationalAlertState(
                alert_key=draft.alert_key,
                alert_type=draft.alert_type,
                severity=draft.severity,
                status="active",
                summary=draft.summary,
                details=dict(draft.details),
                first_triggered_at=now,
                last_evaluated_at=now,
                last_triggered_at=now,
                last_notified_at=None,
                resolved_at=None,
                occurrence_count=1,
                created_at=now,
                updated_at=now,
            )
            session.add(state)
            await session.flush()
            return AlertTransition(action="triggered", state=state)

        state.last_evaluated_at = now
        state.summary = draft.summary
        state.details = dict(draft.details)
        state.severity = draft.severity
        state.updated_at = now
        flag_modified(state, "details")
        if draft.triggered:
            state.last_triggered_at = now
            state.occurrence_count += 1
            if state.status == "resolved":
                state.status = "active"
                state.first_triggered_at = now
                state.resolved_at = None
                return AlertTransition(action="triggered", state=state)
            repeat_due = (
                state.last_notified_at is None
                or now - state.last_notified_at
                >= timedelta(seconds=self.policy.repeat_notification_seconds)
            )
            if repeat_due:
                return AlertTransition(action="repeated", state=state)
            return None

        if state.status == "active":
            state.status = "resolved"
            state.resolved_at = now
            return AlertTransition(action="resolved", state=state)
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
