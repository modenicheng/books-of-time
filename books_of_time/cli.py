from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

from books_of_time.accounts.models import AccountStatus, CredentialSnapshot
from books_of_time.accounts.qr_login import QrLoginFlow
from books_of_time.analysis.comment_flags import (
    CommentFlagAnalyzer,
    CommentFlagRefreshSummary,
)
from books_of_time.analysis.hot_turnover import HotCommentTurnoverAnalyzer
from books_of_time.analysis.keywords import (
    KeywordCooccurrenceAnalyzer,
    KeywordTrendAnalyzer,
)
from books_of_time.analysis.propagation import PropagationNodeAnalyzer
from books_of_time.analysis.replay import (
    CommentVisibilityReplayAnalyzer,
    EventPropagationReplayAnalyzer,
    HotCommentReplayAnalyzer,
    VideoMetricReplayAnalyzer,
)
from books_of_time.analysis.stance import StanceEvidenceAnalyzer, StanceLexicon
from books_of_time.analysis.templates import TemplateCandidateAnalyzer
from books_of_time.analysis.turning_points import TurningPointAnalyzer
from books_of_time.app import (
    build_account_manager,
    build_bilibili_client,
    build_engine,
    build_service_coordinator,
    build_session_factory,
    build_worker,
)
from books_of_time.common.logger import get_logger
from books_of_time.config import load_config
from books_of_time.coverage import EventCoverageSummary
from books_of_time.db.migrations import get_expected_schema_revision
from books_of_time.db.repositories import (
    CollectionCoverageRepository,
    CollectionTaskRepository,
    EventRepository,
    RawPayloadRepository,
    VideoMetricSnapshotRepository,
)
from books_of_time.db.schema import adopt_legacy_schema, create_schema
from books_of_time.domain.enums import TaskKind, TaskStatus
from books_of_time.domain.events import EVENT_STATUSES, EVENT_TARGET_TYPES
from books_of_time.parsers.discovery import parse_user_video_list
from books_of_time.reports.event_report import (
    EventReport,
    EventReportGenerator,
    EventReportOptions,
)
from books_of_time.service.health import ServiceHealthChecker
from books_of_time.service.host import ServiceHost
from books_of_time.service.models import ServiceHealthReport
from books_of_time.storage.factory import build_raw_payload_store
from books_of_time.task_orchestrator.discovery import DiscoveryScheduler
from books_of_time.task_orchestrator.discovery_loop import (
    DiscoveryLoop,
    DiscoveryUidSource,
)
from books_of_time.task_orchestrator.discovery_sources import (
    resolve_discovery_uid_sources,
)

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bot")
    parser.add_argument("--config", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db")
    init_db.add_argument("--adopt-legacy", action="store_true")

    monitor = subparsers.add_parser("monitor-video")
    monitor.add_argument("bvid")
    monitor.add_argument("--priority", type=int, default=100)

    video = subparsers.add_parser("video")
    video_sub = video.add_subparsers(dest="video_command", required=True)
    comments = video_sub.add_parser("comments")
    comments.add_argument("bvid")
    comments.add_argument("--mode", choices=["hot"], default="hot")
    comments.add_argument("--priority", type=int, default=80)
    comments.add_argument("--tier", choices=["s", "a", "b", "c"], default="c")
    comments.add_argument("--page-limit", type=int, default=None)
    stats = video_sub.add_parser("stats")
    stats.add_argument("bvid")
    stats.add_argument("--limit", type=int, default=20)
    turnover = video_sub.add_parser("hot-turnover")
    turnover.add_argument("bvid")
    turnover.add_argument("--since", required=True)
    turnover.add_argument("--until", required=True)
    turnover.add_argument("--top-n", type=int, default=20)
    turnover.add_argument("--output", required=True)
    metric_replay = video_sub.add_parser("replay-metrics")
    metric_replay.add_argument("bvid")
    metric_replay.add_argument("--since", required=True)
    metric_replay.add_argument("--until", required=True)
    metric_replay.add_argument("--max-points", type=int, default=100_000)
    metric_replay.add_argument("--output", required=True)
    hot_replay = video_sub.add_parser("replay-hot-comments")
    hot_replay.add_argument("bvid")
    hot_replay.add_argument("--since", required=True)
    hot_replay.add_argument("--until", required=True)
    hot_replay.add_argument("--top-n", type=int, default=20)
    hot_replay.add_argument("--max-snapshots", type=int, default=10_000)
    hot_replay.add_argument("--output", required=True)
    visibility_replay = video_sub.add_parser("replay-visibility")
    visibility_replay.add_argument("bvid")
    visibility_replay.add_argument("--since", required=True)
    visibility_replay.add_argument("--until", required=True)
    visibility_replay.add_argument("--max-events", type=int, default=100_000)
    visibility_replay.add_argument("--output", required=True)

    latest_comments = subparsers.add_parser("collect-latest-comments")
    latest_comments.add_argument("bvid")
    latest_comments.add_argument("--priority", type=int, default=70)
    latest_comments.add_argument("--max-scan-seconds", type=float, default=55)

    coverage = subparsers.add_parser("coverage")
    coverage.add_argument("bvid")
    coverage.add_argument("--limit", type=int, default=20)

    raw = subparsers.add_parser("raw")
    raw_sub = raw.add_subparsers(dest="raw_command", required=True)
    raw_inspect = raw_sub.add_parser("inspect")
    raw_inspect.add_argument("raw_payload_id", type=int)
    raw_inspect.add_argument("--preview-bytes", type=int, default=1200)

    worker = subparsers.add_parser("worker")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    worker_sub.add_parser("run-once")
    worker_loop = worker_sub.add_parser("loop")
    worker_loop.add_argument("--idle-sleep-seconds", type=float, default=5)
    worker_loop.add_argument("--max-iterations", type=int, default=None)
    worker_loop.add_argument("--stop-when-idle", action="store_true")

    task = subparsers.add_parser("task")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_list = task_sub.add_parser("list")
    task_list.add_argument(
        "--status",
        choices=[status.value for status in TaskStatus],
        default=None,
    )
    task_list.add_argument("--limit", type=int, default=20)
    task_retry_failed = task_sub.add_parser("retry-failed")
    task_retry_failed.add_argument("--target-id", default=None)
    task_retry_failed.add_argument(
        "--kind",
        choices=[kind.value for kind in TaskKind],
        default=None,
    )
    task_retry_failed.add_argument("--limit", type=int, default=100)

    discovery = subparsers.add_parser("discovery")
    discovery_sub = discovery.add_subparsers(dest="discovery_command", required=True)
    discovery_loop = discovery_sub.add_parser("loop")
    discovery_loop.add_argument("--interval-seconds", type=float, default=None)
    discovery_loop.add_argument("--max-iterations", type=int, default=None)
    discovery_loop.add_argument("--stop-when-idle", action="store_true")

    service = subparsers.add_parser("service")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_run = service_sub.add_parser("run")
    service_run.add_argument("--max-worker-iterations", type=int, default=None)
    service_sub.add_parser("doctor")
    service_sub.add_parser("health")
    service_status = service_sub.add_parser("status")
    service_status.add_argument("--limit", type=int, default=20)

    login = subparsers.add_parser("login")
    login_sub = login.add_subparsers(dest="login_command", required=True)
    login_qr = login_sub.add_parser("qr")
    login_qr.add_argument("--account", default="default")
    login_qr.add_argument("--timeout-seconds", type=float, default=180)
    login_status = login_sub.add_parser("status")
    login_status.add_argument("--account", default="default")
    login_logout = login_sub.add_parser("logout")
    login_logout.add_argument("--account", default="default")

    event = subparsers.add_parser("event")
    event_sub = event.add_subparsers(dest="event_command", required=True)
    event_create = event_sub.add_parser("create")
    event_create.add_argument("slug")
    event_create.add_argument("--name", required=True)
    event_create.add_argument("--game", default=None)
    event_create.add_argument("--description", default=None)
    event_create.add_argument(
        "--status",
        choices=sorted(EVENT_STATUSES),
        default="active",
    )
    event_create.add_argument("--start-at", default=None)
    event_create.add_argument("--end-at", default=None)
    event_create.add_argument("--timezone", default="Asia/Shanghai")
    event_list = event_sub.add_parser("list")
    event_list.add_argument("--limit", type=int, default=100)
    event_add_target = event_sub.add_parser("add-target")
    event_add_target.add_argument("event_reference")
    event_add_target.add_argument("target_type", choices=sorted(EVENT_TARGET_TYPES))
    event_add_target.add_argument("target_value")
    event_add_target.add_argument("--priority", type=int, default=0)
    event_add_target.add_argument(
        "--role",
        choices=["official", "major_creator"],
        default=None,
    )
    event_list_videos = event_sub.add_parser("list-videos")
    event_list_videos.add_argument("event_reference")
    event_list_videos.add_argument("--limit", type=int, default=1000)
    event_coverage = event_sub.add_parser("coverage")
    event_coverage.add_argument("event_reference")
    event_export = event_sub.add_parser("export-timeline")
    event_export.add_argument("event_reference")
    event_export.add_argument("--output", required=True)
    event_trends = event_sub.add_parser("keyword-trends")
    event_trends.add_argument("event_reference")
    event_trends.add_argument("--since", required=True)
    event_trends.add_argument("--until", required=True)
    event_trends.add_argument("--bucket-minutes", type=int, default=60)
    event_trends.add_argument("--bvid", default=None)
    event_trends.add_argument("--output", required=True)
    event_cooccurrence = event_sub.add_parser("keyword-cooccurrence")
    event_cooccurrence.add_argument("event_reference")
    event_cooccurrence.add_argument("--since", required=True)
    event_cooccurrence.add_argument("--until", required=True)
    event_cooccurrence.add_argument("--bvid", default=None)
    event_cooccurrence.add_argument("--output", required=True)
    event_stance = event_sub.add_parser("stance-evidence")
    event_stance.add_argument("event_reference")
    event_stance.add_argument("--since", required=True)
    event_stance.add_argument("--until", required=True)
    event_stance.add_argument("--bvid", default=None)
    event_stance.add_argument("--output", required=True)
    event_templates = event_sub.add_parser("template-candidates")
    event_templates.add_argument("event_reference")
    event_templates.add_argument("--since", required=True)
    event_templates.add_argument("--until", required=True)
    event_templates.add_argument("--window-minutes", type=int, default=60)
    event_templates.add_argument("--min-similarity", type=float, default=0.85)
    event_templates.add_argument("--min-text-chars", type=int, default=8)
    event_templates.add_argument("--max-comments", type=int, default=5000)
    event_templates.add_argument("--max-comparisons", type=int, default=100_000)
    event_templates.add_argument("--output", required=True)
    event_flags = event_sub.add_parser("refresh-comment-flags")
    event_flags.add_argument("event_reference")
    event_flags.add_argument("--since", required=True)
    event_flags.add_argument("--until", required=True)
    event_flags.add_argument("--template-window-minutes", type=int, default=60)
    event_flags.add_argument("--template-min-similarity", type=float, default=0.85)
    event_flags.add_argument("--template-min-text-chars", type=int, default=8)
    event_flags.add_argument("--max-comments", type=int, default=5000)
    event_flags.add_argument("--max-comparisons", type=int, default=100_000)
    event_flags.add_argument("--output", required=True)
    event_nodes = event_sub.add_parser("propagation-nodes")
    event_nodes.add_argument("event_reference")
    event_nodes.add_argument("--since", required=True)
    event_nodes.add_argument("--until", required=True)
    event_nodes.add_argument("--max-comments", type=int, default=50_000)
    event_nodes.add_argument("--output", required=True)
    event_turning = event_sub.add_parser("turning-points")
    event_turning.add_argument("event_reference")
    event_turning.add_argument("--since", required=True)
    event_turning.add_argument("--until", required=True)
    event_turning.add_argument("--bucket-minutes", type=int, default=60)
    event_turning.add_argument("--spike-multiplier", type=float, default=3.0)
    event_turning.add_argument("--min-count", type=int, default=5)
    event_turning.add_argument("--turnover-threshold", type=float, default=0.5)
    event_turning.add_argument("--top-n", type=int, default=20)
    event_turning.add_argument("--max-records", type=int, default=200_000)
    event_turning.add_argument("--output", required=True)
    event_replay = event_sub.add_parser("replay-propagation")
    event_replay.add_argument("event_reference")
    event_replay.add_argument("--since", required=True)
    event_replay.add_argument("--until", required=True)
    event_replay.add_argument("--max-records", type=int, default=100_000)
    event_replay.add_argument("--output", required=True)
    event_report = event_sub.add_parser("report")
    event_report.add_argument("event_reference")
    event_report.add_argument("--since", required=True)
    event_report.add_argument("--until", required=True)
    event_report.add_argument("--bucket-minutes", type=int, default=60)
    event_report.add_argument("--spike-multiplier", type=float, default=3.0)
    event_report.add_argument("--spike-min-count", type=int, default=5)
    event_report.add_argument("--turnover-threshold", type=float, default=0.5)
    event_report.add_argument("--top-n", type=int, default=20)
    event_report.add_argument("--template-window-minutes", type=int, default=60)
    event_report.add_argument("--template-min-similarity", type=float, default=0.85)
    event_report.add_argument("--template-min-text-chars", type=int, default=8)
    event_report.add_argument("--max-videos", type=int, default=100)
    event_report.add_argument("--max-records", type=int, default=5000)
    event_report.add_argument("--output", required=True)
    event_report.add_argument("--json-output")

    discover = subparsers.add_parser("discover-user")
    discover.add_argument("mid")
    discover.add_argument("--page", type=int, default=1)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    if args.command == "init-db":
        if args.adopt_legacy:
            await adopt_legacy_schema(args.config)
        else:
            await create_schema(args.config)
        logger.info("Database schema is ready")
        return

    cfg = load_config(args.config)

    if args.command == "service" and args.service_command == "run":
        await _run_service(
            cfg,
            max_worker_iterations=args.max_worker_iterations,
        )
        return

    if args.command == "service" and args.service_command == "doctor":
        await _show_service_doctor(cfg)
        return

    if args.command == "service" and args.service_command == "health":
        await _show_service_health(cfg)
        return

    if args.command == "service" and args.service_command == "status":
        await _show_service_status(cfg, limit=args.limit)
        return

    if args.command == "login" and args.login_command == "qr":
        await _login_qr(
            cfg,
            account_id=args.account,
            timeout_seconds=args.timeout_seconds,
        )
        return

    if args.command == "login" and args.login_command == "status":
        await _show_login_status(cfg, account_id=args.account)
        return

    if args.command == "login" and args.login_command == "logout":
        await _logout_account(cfg, account_id=args.account)
        return

    if args.command == "event" and args.event_command == "create":
        await _create_event(
            cfg,
            slug=args.slug,
            name=args.name,
            game=args.game,
            description=args.description,
            status=args.status,
            start_at=args.start_at,
            end_at=args.end_at,
            timezone=args.timezone,
        )
        return

    if args.command == "event" and args.event_command == "list":
        await _list_events(cfg, limit=args.limit)
        return

    if args.command == "event" and args.event_command == "add-target":
        await _add_event_target(
            cfg,
            event_reference=args.event_reference,
            target_type=args.target_type,
            target_value=args.target_value,
            priority=args.priority,
            role=args.role,
        )
        return

    if args.command == "event" and args.event_command == "list-videos":
        await _list_event_videos(
            cfg,
            event_reference=args.event_reference,
            limit=args.limit,
        )
        return

    if args.command == "event" and args.event_command == "coverage":
        await _show_event_coverage(cfg, event_reference=args.event_reference)
        return

    if args.command == "event" and args.event_command == "export-timeline":
        await _export_event_timeline(
            cfg,
            event_reference=args.event_reference,
            output_path=Path(args.output),
        )
        return

    if args.command == "event" and args.event_command == "keyword-trends":
        await _export_keyword_trends(
            cfg,
            event_reference=args.event_reference,
            since=args.since,
            until=args.until,
            bucket_minutes=args.bucket_minutes,
            bvid=args.bvid,
            output_path=Path(args.output),
        )
        return

    if args.command == "event" and args.event_command == "keyword-cooccurrence":
        await _export_keyword_cooccurrence(
            cfg,
            event_reference=args.event_reference,
            since=args.since,
            until=args.until,
            bvid=args.bvid,
            output_path=Path(args.output),
        )
        return

    if args.command == "event" and args.event_command == "stance-evidence":
        await _export_stance_evidence(
            cfg,
            event_reference=args.event_reference,
            since=args.since,
            until=args.until,
            bvid=args.bvid,
            output_path=Path(args.output),
        )
        return

    if args.command == "event" and args.event_command == "template-candidates":
        await _export_template_candidates(
            cfg,
            event_reference=args.event_reference,
            since=args.since,
            until=args.until,
            window_minutes=args.window_minutes,
            min_similarity=args.min_similarity,
            min_text_chars=args.min_text_chars,
            max_comments=args.max_comments,
            max_comparisons=args.max_comparisons,
            output_path=Path(args.output),
        )
        return

    if args.command == "event" and args.event_command == "refresh-comment-flags":
        await _refresh_comment_flags(
            cfg,
            event_reference=args.event_reference,
            since=args.since,
            until=args.until,
            template_window_minutes=args.template_window_minutes,
            template_min_similarity=args.template_min_similarity,
            template_min_text_chars=args.template_min_text_chars,
            max_comments=args.max_comments,
            max_comparisons=args.max_comparisons,
            output_path=Path(args.output),
        )
        return

    if args.command == "event" and args.event_command == "propagation-nodes":
        await _export_propagation_nodes(
            cfg,
            event_reference=args.event_reference,
            since=args.since,
            until=args.until,
            max_comments=args.max_comments,
            output_path=Path(args.output),
        )
        return

    if args.command == "event" and args.event_command == "turning-points":
        await _export_turning_points(
            cfg,
            event_reference=args.event_reference,
            since=args.since,
            until=args.until,
            bucket_minutes=args.bucket_minutes,
            spike_multiplier=args.spike_multiplier,
            min_count=args.min_count,
            turnover_threshold=args.turnover_threshold,
            top_n=args.top_n,
            max_records=args.max_records,
            output_path=Path(args.output),
        )
        return

    if args.command == "event" and args.event_command == "replay-propagation":
        await _export_propagation_replay(
            cfg,
            event_reference=args.event_reference,
            since=args.since,
            until=args.until,
            max_records=args.max_records,
            output_path=Path(args.output),
        )
        return

    if args.command == "event" and args.event_command == "report":
        await _export_event_report(
            cfg,
            event_reference=args.event_reference,
            since=args.since,
            until=args.until,
            bucket_minutes=args.bucket_minutes,
            spike_multiplier=args.spike_multiplier,
            spike_min_count=args.spike_min_count,
            turnover_threshold=args.turnover_threshold,
            top_n=args.top_n,
            template_window_minutes=args.template_window_minutes,
            template_min_similarity=args.template_min_similarity,
            template_min_text_chars=args.template_min_text_chars,
            max_videos=args.max_videos,
            max_records=args.max_records,
            output_path=Path(args.output),
            json_output_path=(
                Path(args.json_output) if args.json_output is not None else None
            ),
        )
        return

    if args.command == "monitor-video":
        await _monitor_video(cfg, args.bvid, args.priority)
        return

    if args.command == "video" and args.video_command == "comments":
        await _enqueue_video_comments(
            cfg,
            args.bvid,
            args.mode,
            args.priority,
            args.tier,
            args.page_limit,
        )
        return

    if args.command == "video" and args.video_command == "stats":
        await _show_video_stats(cfg, args.bvid, args.limit)
        return

    if args.command == "video" and args.video_command == "hot-turnover":
        await _export_hot_turnover(
            cfg,
            bvid=args.bvid,
            since=args.since,
            until=args.until,
            top_n=args.top_n,
            output_path=Path(args.output),
        )
        return

    if args.command == "video" and args.video_command == "replay-metrics":
        await _export_video_metric_replay(
            cfg,
            bvid=args.bvid,
            since=args.since,
            until=args.until,
            max_points=args.max_points,
            output_path=Path(args.output),
        )
        return

    if args.command == "video" and args.video_command == "replay-hot-comments":
        await _export_hot_comment_replay(
            cfg,
            bvid=args.bvid,
            since=args.since,
            until=args.until,
            top_n=args.top_n,
            max_snapshots=args.max_snapshots,
            output_path=Path(args.output),
        )
        return

    if args.command == "video" and args.video_command == "replay-visibility":
        await _export_comment_visibility_replay(
            cfg,
            bvid=args.bvid,
            since=args.since,
            until=args.until,
            max_events=args.max_events,
            output_path=Path(args.output),
        )
        return

    if args.command == "collect-latest-comments":
        await _enqueue_latest_comments(
            cfg,
            args.bvid,
            args.priority,
            args.max_scan_seconds,
        )
        return

    if args.command == "coverage":
        await _show_coverage(cfg, args.bvid, args.limit)
        return

    if args.command == "raw" and args.raw_command == "inspect":
        await _inspect_raw_payload(cfg, args.raw_payload_id, args.preview_bytes)
        return

    if args.command == "worker" and args.worker_command == "run-once":
        worker = build_worker(
            cfg,
            run_id=f"cli-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}",
            lease_owner="cli-worker",
        )
        executed = await worker.run_once()
        logger.info("Worker executed task: %s", executed)
        return

    if args.command == "worker" and args.worker_command == "loop":
        worker = build_worker(
            cfg,
            run_id=f"cli-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}",
            lease_owner="cli-worker",
        )
        executed = await worker.run_loop(
            idle_sleep_seconds=args.idle_sleep_seconds,
            max_iterations=args.max_iterations,
            stop_when_idle=args.stop_when_idle,
        )
        logger.info("Worker loop executed tasks: %s", executed)
        return

    if args.command == "task" and args.task_command == "list":
        await _list_tasks(cfg, args.status, args.limit)
        return

    if args.command == "task" and args.task_command == "retry-failed":
        await _retry_failed_tasks(cfg, args.target_id, args.kind, args.limit)
        return

    if args.command == "discovery" and args.discovery_command == "loop":
        await _run_discovery_loop(
            cfg,
            interval_seconds=args.interval_seconds,
            max_iterations=args.max_iterations,
            stop_when_idle=args.stop_when_idle,
        )
        return

    if args.command == "discover-user":
        await _discover_user(cfg, args.mid, args.page)
        return

    raise ValueError(f"Unsupported command: {args.command}")


async def _run_service(
    cfg: dict,
    *,
    max_worker_iterations: int | None,
) -> None:
    engine = build_engine(cfg)
    session_factory = build_session_factory(cfg, engine=engine)
    checker = _build_service_health_checker(cfg, session_factory)
    service_cfg = cfg.get("service", {})
    roles = [str(role) for role in service_cfg.get("roles", ["worker", "scheduler"])]
    if "worker" not in roles:
        await engine.dispose()
        raise ValueError("Service-1 requires the worker role")

    doctor = await checker.doctor()
    _log_health_report(doctor)
    if not doctor.ok:
        await engine.dispose()
        raise RuntimeError("Service startup checks failed")

    client = build_bilibili_client(cfg)
    instance_prefix = str(service_cfg.get("instance_id") or socket.gethostname())
    instance_id = f"{instance_prefix}-{os.getpid()}-{uuid4().hex[:8]}"
    run_id = f"service-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    worker = build_worker(
        cfg,
        run_id=run_id,
        lease_owner=instance_id,
        session_factory=session_factory,
        client=client,
    )
    coordinator = None
    if "scheduler" in roles:
        coordinator = build_service_coordinator(
            cfg,
            session_factory=session_factory,
            instance_id=instance_id,
            client=client,
        )
        await coordinator.bootstrap(now=datetime.now(UTC))
    host = ServiceHost(
        session_factory=session_factory,
        worker=worker,
        coordinator=coordinator,
        instance_id=instance_id,
        roles=roles,
        hostname=socket.gethostname(),
        pid=os.getpid(),
        version=_application_version(),
        heartbeat_seconds=float(service_cfg.get("heartbeat_seconds", 10)),
        shutdown_grace_seconds=float(service_cfg.get("shutdown_grace_seconds", 60)),
        worker_idle_sleep_seconds=float(
            service_cfg.get("worker_idle_sleep_seconds", 5)
        ),
    )
    cleanup_signals = _install_service_signal_handlers(host)
    try:
        executed = await host.run(max_worker_iterations=max_worker_iterations)
        logger.info(
            "Service stopped instance=%s executed_tasks=%s",
            instance_id,
            executed,
        )
    finally:
        cleanup_signals()
        await engine.dispose()


async def _show_service_doctor(cfg: dict) -> None:
    engine = build_engine(cfg)
    try:
        checker = _build_service_health_checker(
            cfg,
            build_session_factory(cfg, engine=engine),
        )
        report = await checker.doctor()
        _log_health_report(report)
    finally:
        await engine.dispose()
    if not report.ok:
        raise SystemExit(1)


async def _show_service_health(cfg: dict) -> None:
    engine = build_engine(cfg)
    try:
        checker = _build_service_health_checker(
            cfg,
            build_session_factory(cfg, engine=engine),
        )
        report = await checker.health(now=datetime.now(UTC))
        _log_health_report(report)
    finally:
        await engine.dispose()
    if not report.ok:
        raise SystemExit(1)


async def _show_service_status(cfg: dict, *, limit: int) -> None:
    engine = build_engine(cfg)
    try:
        checker = _build_service_health_checker(
            cfg,
            build_session_factory(cfg, engine=engine),
        )
        status = await checker.status(
            now=datetime.now(UTC),
            instance_limit=min(max(limit, 1), 200),
        )
    finally:
        await engine.dispose()

    logger.info(
        "Service queue pending=%s running=%s failed=%s oldest_pending_at=%s "
        "active_backoffs=%s request_window_since=%s pages_requested=%s "
        "request_errors=%s request_failure_rate=%s parse_errors=%s",
        status.pending_tasks,
        status.running_tasks,
        status.failed_tasks,
        status.oldest_pending_at.isoformat()
        if status.oldest_pending_at is not None
        else None,
        status.active_backoffs,
        status.request_failures.since_at.isoformat(),
        status.request_failures.pages_requested,
        status.request_failures.request_errors,
        status.request_failures.request_failure_rate,
        status.request_failures.parse_errors,
    )
    for instance in status.instances:
        logger.info(
            "Service instance=%s host=%s pid=%s roles=%s status=%s "
            "started_at=%s heartbeat_at=%s stopped_at=%s error_type=%s",
            instance.instance_id,
            instance.hostname,
            instance.pid,
            ",".join(instance.roles),
            instance.status,
            instance.started_at.isoformat(),
            instance.heartbeat_at.isoformat(),
            instance.stopped_at.isoformat()
            if instance.stopped_at is not None
            else None,
            instance.last_error_type,
        )


def _build_service_health_checker(cfg: dict, session_factory) -> ServiceHealthChecker:
    storage_cfg = cfg.get("storage", {})
    service_cfg = cfg.get("service", {})
    return ServiceHealthChecker(
        session_factory=session_factory,
        raw_store=build_raw_payload_store(cfg),
        media_dir=storage_cfg.get("media_dir", "./data/media"),
        heartbeat_timeout_seconds=float(
            service_cfg.get("heartbeat_timeout_seconds", 30)
        ),
        request_failure_window_seconds=int(
            service_cfg.get("request_failure_window_seconds", 3600)
        ),
        expected_schema_revision=get_expected_schema_revision(),
    )


def _log_health_report(report: ServiceHealthReport) -> None:
    for check in report.checks:
        log = logger.info if check.ok else logger.error
        log("Service check %s ok=%s detail=%s", check.name, check.ok, check.detail)


def _application_version() -> str:
    try:
        return version("books-of-time")
    except PackageNotFoundError:
        return "0.1.0"


def _install_service_signal_handlers(host: ServiceHost) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    loop_signals: list[signal.Signals] = []
    fallback_handlers: dict[signal.Signals, signal.Handlers] = {}

    def request_stop(signum=None, frame=None) -> None:
        loop.call_soon_threadsafe(host.request_stop)

    for candidate in _service_stop_signals():
        try:
            loop.add_signal_handler(candidate, host.request_stop)
            loop_signals.append(candidate)
        except (NotImplementedError, RuntimeError):
            fallback_handlers[candidate] = signal.getsignal(candidate)
            signal.signal(candidate, request_stop)

    def cleanup() -> None:
        for candidate in loop_signals:
            loop.remove_signal_handler(candidate)
        for candidate, previous in fallback_handlers.items():
            signal.signal(candidate, previous)

    return cleanup


def _service_stop_signals() -> tuple[signal.Signals, ...]:
    candidates = [signal.SIGINT, signal.SIGTERM]
    windows_break = getattr(signal, "SIGBREAK", None)
    if windows_break is not None:
        candidates.append(windows_break)
    return tuple(candidates)


def _parse_event_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Event datetime must include a timezone offset")
    return parsed.astimezone(UTC)


async def _login_qr(
    cfg: dict,
    *,
    account_id: str,
    timeout_seconds: float,
) -> CredentialSnapshot:
    client = build_bilibili_client(cfg)
    return await QrLoginFlow(
        manager=build_account_manager(cfg),
        http_client=client.http_client,
        rate_limiter=client.rate_limiter,
    ).run(
        account_id=account_id,
        timeout_seconds=timeout_seconds,
    )


async def _show_login_status(
    cfg: dict,
    *,
    account_id: str,
) -> AccountStatus | None:
    status = build_account_manager(cfg).status(account_id)
    if status is None:
        logger.info("account=%s status=anonymous", account_id)
        return None
    logger.info(
        "account=%s health=%s source=%s snapshot=%s created_at=%s last_checked_at=%s",
        status.account_id,
        status.health.value,
        status.source,
        status.snapshot_id,
        status.created_at.isoformat(),
        status.last_checked_at.isoformat()
        if status.last_checked_at is not None
        else None,
    )
    return status


async def _logout_account(cfg: dict, *, account_id: str) -> bool:
    removed = build_account_manager(cfg).logout(account_id)
    logger.info(
        "account=%s logout=%s",
        account_id,
        "completed" if removed else "already_anonymous",
    )
    return removed


async def _create_event(
    cfg: dict,
    *,
    slug: str,
    name: str,
    game: str | None,
    description: str | None,
    status: str,
    start_at: str | None,
    end_at: str | None,
    timezone: str,
):
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        event = await EventRepository(session).create_event(
            slug=slug,
            name=name,
            game=game,
            description=description,
            status=status,
            start_at=_parse_event_datetime(start_at),
            end_at=_parse_event_datetime(end_at),
            timezone=timezone,
            now=datetime.now(UTC),
        )
        await session.commit()
    logger.info(
        "Created event id=%s slug=%s name=%s status=%s",
        event.id,
        event.slug,
        event.name,
        event.status,
    )
    return event


async def _list_events(cfg: dict, *, limit: int) -> None:
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        events = await EventRepository(session).list_events(limit=limit)
    if not events:
        logger.info("No events found")
        return
    for event in events:
        logger.info(
            "event id=%s slug=%s name=%s game=%s status=%s start_at=%s end_at=%s",
            event.id,
            event.slug,
            event.name,
            event.game,
            event.status,
            event.start_at.isoformat() if event.start_at is not None else None,
            event.end_at.isoformat() if event.end_at is not None else None,
        )


async def _add_event_target(
    cfg: dict,
    *,
    event_reference: str,
    target_type: str,
    target_value: str,
    priority: int,
    role: str | None = None,
):
    if role is not None and target_type != "uid":
        raise ValueError("Event target roles are only supported for uid targets")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.resolve_event(event_reference)
        target = await repository.add_target(
            event_id=event.id,
            target_type=target_type,
            target_value=target_value,
            priority=priority,
            extra={"role": role} if role is not None else None,
            now=datetime.now(UTC),
        )
        await session.commit()
    logger.info(
        "Registered event target event=%s type=%s value=%s priority=%s role=%s",
        event.slug,
        target.target_type,
        target.target_value,
        target.priority,
        target.extra.get("role"),
    )
    return target


async def _list_event_videos(
    cfg: dict,
    *,
    event_reference: str,
    limit: int,
) -> None:
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        repository = EventRepository(session)
        event = await repository.resolve_event(event_reference)
        videos = await repository.list_videos(event.id, limit=limit)
    if not videos:
        logger.info("No videos found for event id=%s slug=%s", event.id, event.slug)
        return
    for video in videos:
        logger.info(
            "event_video event=%s bvid=%s reason=%s confidence=%s first_seen_at=%s",
            event.slug,
            video.bvid,
            video.association_reason,
            video.confidence,
            video.first_seen_at.isoformat(),
        )


async def _show_event_coverage(
    cfg: dict,
    *,
    event_reference: str,
) -> EventCoverageSummary:
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        summary = await EventRepository(session).get_coverage_summary(event_reference)
    logger.info(
        "event_coverage event=%s videos=%s rows=%s statuses=%s/%s/%s "
        "pages=%s items=%s raw=%s errors=parse:%s,request:%s "
        "truncated=%s corrupted=%s first_started_at=%s last_finished_at=%s",
        summary.event_slug,
        _format_ratio(
            summary.videos_with_coverage,
            summary.active_video_count,
            summary.video_coverage_ratio,
        ),
        summary.coverage_row_count,
        summary.succeeded_count,
        summary.partial_count,
        summary.failed_count,
        _format_ratio(
            summary.pages_succeeded,
            summary.pages_requested,
            summary.page_success_rate,
        ),
        summary.items_observed,
        summary.raw_payloads_saved,
        summary.parse_errors,
        summary.request_errors,
        summary.truncated_count,
        summary.corrupted_count,
        summary.first_started_at.isoformat()
        if summary.first_started_at is not None
        else None,
        summary.last_finished_at.isoformat()
        if summary.last_finished_at is not None
        else None,
    )
    return summary


def _format_ratio(numerator: int, denominator: int, ratio: float | None) -> str:
    percentage = f"{ratio:.1%}" if ratio is not None else "n/a"
    return f"{numerator}/{denominator} ({percentage})"


async def _export_event_timeline(
    cfg: dict,
    *,
    event_reference: str,
    output_path: Path,
) -> int:
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        rows = await EventRepository(session).build_timeline(event_reference)

    _write_jsonl_atomic(output_path, [row.as_dict() for row in rows])
    logger.info(
        "Exported event timeline event=%s rows=%s output=%s",
        event_reference,
        len(rows),
        output_path,
    )
    return len(rows)


async def _export_keyword_trends(
    cfg: dict,
    *,
    event_reference: str,
    since: str,
    until: str,
    bucket_minutes: int,
    bvid: str | None,
    output_path: Path,
) -> int:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Keyword trend window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        points = await KeywordTrendAnalyzer(session).analyze(
            event_reference=event_reference,
            since=since_at,
            until=until_at,
            bucket_seconds=bucket_minutes * 60,
            bvid=bvid,
        )

    _write_jsonl_atomic(output_path, [point.as_dict() for point in points])
    logger.info(
        "Exported keyword trends event=%s bvid=%s points=%s output=%s",
        event_reference,
        bvid,
        len(points),
        output_path,
    )
    return len(points)


async def _export_keyword_cooccurrence(
    cfg: dict,
    *,
    event_reference: str,
    since: str,
    until: str,
    bvid: str | None,
    output_path: Path,
) -> int:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Keyword co-occurrence window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        edges = await KeywordCooccurrenceAnalyzer(session).analyze(
            event_reference=event_reference,
            since=since_at,
            until=until_at,
            bvid=bvid,
        )

    _write_jsonl_atomic(output_path, [edge.as_dict() for edge in edges])
    logger.info(
        "Exported keyword co-occurrence event=%s bvid=%s edges=%s output=%s",
        event_reference,
        bvid,
        len(edges),
        output_path,
    )
    return len(edges)


async def _export_stance_evidence(
    cfg: dict,
    *,
    event_reference: str,
    since: str,
    until: str,
    bvid: str | None,
    output_path: Path,
) -> int:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Stance evidence window requires since and until")
    analysis_cfg = cfg.get("analysis")
    if not isinstance(analysis_cfg, dict):
        raise ValueError("Configuration section analysis must be a mapping")
    lexicon = StanceLexicon.from_config(analysis_cfg.get("stance_lexicon", {}))
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        rows = await StanceEvidenceAnalyzer(session).analyze(
            event_reference=event_reference,
            since=since_at,
            until=until_at,
            lexicon=lexicon,
            bvid=bvid,
        )

    _write_jsonl_atomic(output_path, [row.as_dict() for row in rows])
    logger.info(
        "Exported stance evidence event=%s bvid=%s lexicon=%s rows=%s output=%s",
        event_reference,
        bvid,
        lexicon.version,
        len(rows),
        output_path,
    )
    return len(rows)


async def _export_template_candidates(
    cfg: dict,
    *,
    event_reference: str,
    since: str,
    until: str,
    window_minutes: int,
    min_similarity: float,
    min_text_chars: int,
    max_comments: int,
    max_comparisons: int,
    output_path: Path,
) -> int:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Template candidate window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        rows = await TemplateCandidateAnalyzer(session).analyze(
            event_reference=event_reference,
            since=since_at,
            until=until_at,
            window_seconds=window_minutes * 60,
            min_similarity=min_similarity,
            min_text_chars=min_text_chars,
            max_comments=max_comments,
            max_comparisons=max_comparisons,
        )

    _write_jsonl_atomic(output_path, [row.as_dict() for row in rows])
    logger.info(
        "Exported template candidates event=%s candidates=%s output=%s",
        event_reference,
        len(rows),
        output_path,
    )
    return len(rows)


async def _refresh_comment_flags(
    cfg: dict,
    *,
    event_reference: str,
    since: str,
    until: str,
    template_window_minutes: int,
    template_min_similarity: float,
    template_min_text_chars: int,
    max_comments: int,
    max_comparisons: int,
    output_path: Path,
) -> CommentFlagRefreshSummary:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Comment flag window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        summary = await CommentFlagAnalyzer(session).refresh(
            event_reference=event_reference,
            since=since_at,
            until=until_at,
            detected_at=datetime.now(UTC),
            template_window_seconds=template_window_minutes * 60,
            template_min_similarity=template_min_similarity,
            template_min_text_chars=template_min_text_chars,
            max_comments=max_comments,
            max_comparisons=max_comparisons,
        )
        await session.commit()

    _write_jsonl_atomic(output_path, [summary.as_dict()])
    logger.info(
        "Refreshed comment flags event=%s matched=%s created=%s output=%s",
        event_reference,
        summary.matched_count,
        summary.created_count,
        output_path,
    )
    return summary


async def _export_propagation_nodes(
    cfg: dict,
    *,
    event_reference: str,
    since: str,
    until: str,
    max_comments: int,
    output_path: Path,
) -> int:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Propagation node window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        rows = await PropagationNodeAnalyzer(session).analyze(
            event_reference=event_reference,
            since=since_at,
            until=until_at,
            max_comments=max_comments,
        )

    _write_jsonl_atomic(output_path, [row.as_dict() for row in rows])
    logger.info(
        "Exported propagation nodes event=%s rows=%s output=%s",
        event_reference,
        len(rows),
        output_path,
    )
    return len(rows)


async def _export_turning_points(
    cfg: dict,
    *,
    event_reference: str,
    since: str,
    until: str,
    bucket_minutes: int,
    spike_multiplier: float,
    min_count: int,
    turnover_threshold: float,
    top_n: int,
    output_path: Path,
    max_records: int = 200_000,
) -> int:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Turning point window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        rows = await TurningPointAnalyzer(session).analyze(
            event_reference=event_reference,
            since=since_at,
            until=until_at,
            bucket_seconds=bucket_minutes * 60,
            spike_multiplier=spike_multiplier,
            min_count=min_count,
            turnover_threshold=turnover_threshold,
            top_n=top_n,
            max_records=max_records,
        )

    _write_jsonl_atomic(output_path, [row.as_dict() for row in rows])
    logger.info(
        "Exported turning points event=%s signals=%s output=%s",
        event_reference,
        len(rows),
        output_path,
    )
    return len(rows)


async def _export_propagation_replay(
    cfg: dict,
    *,
    event_reference: str,
    since: str,
    until: str,
    max_records: int,
    output_path: Path,
) -> int:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Propagation replay window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        rows = await EventPropagationReplayAnalyzer(session).analyze(
            event_reference=event_reference,
            since=since_at,
            until=until_at,
            max_records=max_records,
        )
    _write_jsonl_atomic(output_path, [row.as_dict() for row in rows])
    logger.info(
        "Exported propagation replay event=%s records=%s output=%s",
        event_reference,
        len(rows),
        output_path,
    )
    return len(rows)


async def _export_event_report(
    cfg: dict,
    *,
    event_reference: str,
    since: str,
    until: str,
    bucket_minutes: int,
    spike_multiplier: float,
    spike_min_count: int,
    turnover_threshold: float,
    top_n: int,
    template_window_minutes: int,
    template_min_similarity: float,
    template_min_text_chars: int,
    max_videos: int,
    max_records: int,
    output_path: Path,
    json_output_path: Path | None,
) -> EventReport:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Event report window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        report = await EventReportGenerator(session).generate(
            event_reference=event_reference,
            since=since_at,
            until=until_at,
            options=EventReportOptions(
                bucket_seconds=bucket_minutes * 60,
                hot_top_n=top_n,
                spike_multiplier=spike_multiplier,
                spike_min_count=spike_min_count,
                turnover_threshold=turnover_threshold,
                template_window_seconds=template_window_minutes * 60,
                template_min_similarity=template_min_similarity,
                template_min_text_chars=template_min_text_chars,
                max_videos=max_videos,
                max_records=max_records,
            ),
        )
    _write_text_atomic(output_path, report.render_markdown())
    if json_output_path is not None:
        _write_json_atomic(json_output_path, report.as_dict())
    logger.info(
        "Exported event report event=%s output=%s", event_reference, output_path
    )
    return report


def _write_text_atomic(output_path: Path, content: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(content, encoding="utf-8", newline="\n")
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_json_atomic(output_path: Path, record: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_jsonl_atomic(output_path: Path, records: list[dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output:
            for record in records:
                output.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                output.write("\n")
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


async def _export_hot_turnover(
    cfg: dict,
    *,
    bvid: str,
    since: str,
    until: str,
    top_n: int,
    output_path: Path,
) -> int:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Hot turnover window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        points = await HotCommentTurnoverAnalyzer(session).analyze(
            bvid=bvid,
            since=since_at,
            until=until_at,
            top_n=top_n,
        )
    _write_jsonl_atomic(output_path, [point.as_dict() for point in points])
    logger.info(
        "Exported hot turnover bvid=%s top_n=%s points=%s output=%s",
        bvid,
        top_n,
        len(points),
        output_path,
    )
    return len(points)


async def _export_video_metric_replay(
    cfg: dict,
    *,
    bvid: str,
    since: str,
    until: str,
    max_points: int,
    output_path: Path,
) -> int:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Metric replay window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        points = await VideoMetricReplayAnalyzer(session).analyze(
            bvid=bvid,
            since=since_at,
            until=until_at,
            max_points=max_points,
        )
    _write_jsonl_atomic(output_path, [point.as_dict() for point in points])
    logger.info(
        "Exported video metric replay bvid=%s points=%s output=%s",
        bvid,
        len(points),
        output_path,
    )
    return len(points)


async def _export_hot_comment_replay(
    cfg: dict,
    *,
    bvid: str,
    since: str,
    until: str,
    top_n: int,
    max_snapshots: int,
    output_path: Path,
) -> int:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Hot comment replay window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        snapshots = await HotCommentReplayAnalyzer(session).analyze(
            bvid=bvid,
            since=since_at,
            until=until_at,
            top_n=top_n,
            max_snapshots=max_snapshots,
        )
    _write_jsonl_atomic(output_path, [snapshot.as_dict() for snapshot in snapshots])
    logger.info(
        "Exported hot comment replay bvid=%s snapshots=%s output=%s",
        bvid,
        len(snapshots),
        output_path,
    )
    return len(snapshots)


async def _export_comment_visibility_replay(
    cfg: dict,
    *,
    bvid: str,
    since: str,
    until: str,
    max_events: int,
    output_path: Path,
) -> int:
    since_at = _parse_event_datetime(since)
    until_at = _parse_event_datetime(until)
    if since_at is None or until_at is None:
        raise ValueError("Comment visibility replay window requires since and until")
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        events = await CommentVisibilityReplayAnalyzer(session).analyze(
            bvid=bvid,
            since=since_at,
            until=until_at,
            max_events=max_events,
        )
    _write_jsonl_atomic(output_path, [event.as_dict() for event in events])
    logger.info(
        "Exported comment visibility replay bvid=%s events=%s output=%s",
        bvid,
        len(events),
        output_path,
    )
    return len(events)


async def _monitor_video(cfg: dict, bvid: str, priority: int) -> None:
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_VIDEO_STATS,
            target_type="video",
            target_id=bvid,
            priority=priority,
            payload={"bvid": bvid, "reason": "manual_monitor"},
            not_before=datetime.now(UTC),
            idempotency_key=f"{TaskKind.FETCH_VIDEO_STATS.value}:video:{bvid}:manual",
        )
        await session.commit()
    logger.info("Queued video stats task for %s", bvid)


async def _enqueue_video_comments(
    cfg: dict,
    bvid: str,
    mode: str,
    priority: int,
    tier: str = "c",
    page_limit: int | None = None,
) -> None:
    if mode != "hot":
        raise ValueError(f"Unsupported comment mode: {mode}")

    session_factory = build_session_factory(cfg)
    effective_page_limit = page_limit or _hot_comment_page_limit(cfg, tier)
    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id=bvid,
            priority=priority,
            payload={
                "bvid": bvid,
                "mode": mode,
                "page": 1,
                "tier": tier,
                "page_limit": effective_page_limit,
            },
            not_before=datetime.now(UTC),
            idempotency_key=(
                f"{TaskKind.FETCH_HOT_COMMENTS.value}:video:{bvid}:hot:{tier}"
            ),
        )
        await session.commit()
    logger.info("Queued hot comments task for %s", bvid)


def _hot_comment_page_limit(cfg: dict, tier: str) -> int:
    tier_cfg = cfg.get("request_budget", {}).get(tier, {})
    return max(int(tier_cfg.get("hot_pages", 1)), 1)


async def _enqueue_latest_comments(
    cfg: dict,
    bvid: str,
    priority: int,
    max_scan_seconds: float,
) -> None:
    session_factory = build_session_factory(cfg)
    payload = {"bvid": bvid, "mode": "latest"}
    if max_scan_seconds != 55:
        payload["max_scan_seconds"] = max_scan_seconds
    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type="video",
            target_id=bvid,
            priority=priority,
            payload=payload,
            not_before=datetime.now(UTC),
            idempotency_key=(
                f"{TaskKind.FETCH_LATEST_COMMENTS.value}:video:{bvid}:manual"
            ),
        )
        await session.commit()
    logger.info("Queued latest comments task for %s", bvid)


async def _show_coverage(cfg: dict, bvid: str, limit: int) -> None:
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        rows = await CollectionCoverageRepository(session).list_for_target(
            target_type="video",
            target_id=bvid,
            limit=limit,
        )

    if not rows:
        logger.info("No coverage rows for %s", bvid)
        return

    for row in rows:
        logger.info(
            "%s %s status=%s reason=%s pages=%s/%s items=%s "
            "frontier_reached=%s frontier_missing=%s truncated=%s corrupted=%s",
            row.finished_at.isoformat(),
            row.task_kind,
            row.status,
            row.reason,
            row.pages_succeeded,
            row.pages_requested,
            row.items_observed,
            row.frontier_reached,
            row.frontier_missing,
            row.truncated,
            row.corrupted,
        )


async def _show_video_stats(cfg: dict, bvid: str, limit: int) -> None:
    session_factory = build_session_factory(cfg)
    capped_limit = min(max(limit, 1), 200)
    async with session_factory() as session:
        rows = await VideoMetricSnapshotRepository(session).list_for_bvid(
            bvid=bvid,
            limit=capped_limit,
        )

    if not rows:
        logger.info("No video stats snapshots for %s", bvid)
        return

    for row in rows:
        logger.info(
            "%s bvid=%s view=%s like=%s coin=%s favorite=%s share=%s "
            "reply=%s danmaku=%s raw_payload_id=%s",
            row.captured_at.isoformat(),
            row.bvid,
            row.view_count,
            row.like_count,
            row.coin_count,
            row.favorite_count,
            row.share_count,
            row.reply_count,
            row.danmaku_count,
            row.raw_payload_id,
        )


async def _inspect_raw_payload(
    cfg: dict,
    raw_payload_id: int,
    preview_bytes: int,
) -> None:
    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        raw = await RawPayloadRepository(session).get(raw_payload_id)

    if raw is None:
        logger.info("Raw payload not found: %s", raw_payload_id)
        return

    body = build_raw_payload_store(cfg).read_uri(raw.storage_uri)
    clamped_preview_bytes = min(max(preview_bytes, 0), 10000)
    preview = body[:clamped_preview_bytes].decode("utf-8", errors="replace")

    logger.info(
        "raw id=%s request_type=%s captured_at=%s status_code=%s "
        "storage_uri=%s compressed_size=%s uncompressed_size=%s "
        "payload_hash=%s parser_version=%s",
        raw.id,
        raw.request_type,
        raw.captured_at.isoformat(),
        raw.status_code,
        raw.storage_uri,
        raw.compressed_size,
        raw.uncompressed_size,
        raw.payload_hash.hex(),
        raw.parser_version,
    )
    logger.info("raw preview=%s", preview)


async def _list_tasks(cfg: dict, status: str | None, limit: int) -> None:
    session_factory = build_session_factory(cfg)
    status_filter = TaskStatus(status) if status is not None else None
    capped_limit = min(max(limit, 1), 200)
    async with session_factory() as session:
        rows = await CollectionTaskRepository(session).list_tasks(
            status=status_filter,
            limit=capped_limit,
        )

    if not rows:
        logger.info("No tasks found")
        return

    for row in rows:
        logger.info(
            "task id=%s kind=%s target=%s:%s status=%s priority=%s "
            "retries=%s/%s not_before=%s lease_owner=%s lease_until=%s",
            row.id,
            row.kind,
            row.target_type,
            row.target_id,
            row.status,
            row.priority,
            row.retry_count,
            row.max_retries,
            row.not_before.isoformat(),
            row.lease_owner,
            row.lease_until.isoformat() if row.lease_until is not None else None,
        )


async def _retry_failed_tasks(
    cfg: dict,
    target_id: str | None,
    kind: str | None,
    limit: int,
) -> None:
    session_factory = build_session_factory(cfg)
    kind_filter = TaskKind(kind) if kind is not None else None
    capped_limit = min(max(limit, 1), 500)
    async with session_factory() as session:
        retried = await CollectionTaskRepository(session).retry_failed(
            now=datetime.now(UTC),
            target_id=target_id,
            kind=kind_filter,
            limit=capped_limit,
        )
        await session.commit()

    logger.info("Retried failed tasks: %s", retried)


async def _run_discovery_loop(
    cfg: dict,
    *,
    interval_seconds: float | None,
    max_iterations: int | None,
    stop_when_idle: bool,
) -> None:
    scheduler_cfg = cfg.get("scheduler", {})
    discovery_cfg = cfg.get("discovery", {})
    uid_sources = _resolve_discovery_uid_sources(discovery_cfg)
    effective_interval = (
        float(interval_seconds)
        if interval_seconds is not None
        else float(scheduler_cfg.get("discovery_scan_seconds", 60))
    )
    loop = DiscoveryLoop(
        session_factory=build_session_factory(cfg),
        client=build_bilibili_client(cfg),
        uid_sources=uid_sources,
    )
    result = await loop.run_loop(
        interval_seconds=effective_interval,
        max_iterations=max_iterations,
        stop_when_idle=stop_when_idle,
    )
    logger.info(
        "Discovery loop scanned_uids=%s videos_seen=%s videos_created=%s errors=%s",
        result.uids_scanned,
        result.videos_seen,
        result.videos_created,
        result.errors,
    )


def _resolve_discovery_uid_sources(discovery_cfg: dict) -> list[DiscoveryUidSource]:
    return resolve_discovery_uid_sources(discovery_cfg)


async def _discover_user(cfg: dict, mid: str, page: int) -> None:
    client = build_bilibili_client(cfg)
    result = await client.get_user_video_list(mid=mid, page=page)
    payload = json.loads(result.body)
    videos = parse_user_video_list(payload, source_mid=mid)

    session_factory = build_session_factory(cfg)
    scheduler = DiscoveryScheduler(session_factory=session_factory)
    async with session_factory() as session:
        created = await scheduler.handle_discovered_videos(
            session=session,
            videos=videos,
            now=datetime.now(UTC),
        )
        await session.commit()

    logger.info("Discovered %d fresh videos: %s", len(created), ", ".join(created))


if __name__ == "__main__":
    main()
