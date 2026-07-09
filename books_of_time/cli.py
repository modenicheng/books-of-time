from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from books_of_time.app import build_bilibili_client, build_session_factory, build_worker
from books_of_time.common.logger import get_logger
from books_of_time.config import load_config
from books_of_time.db.repositories import (
    CollectionCoverageRepository,
    CollectionTaskRepository,
    RawPayloadRepository,
)
from books_of_time.db.schema import create_schema
from books_of_time.domain.enums import TaskKind, TaskStatus
from books_of_time.parsers.discovery import parse_user_video_list
from books_of_time.storage.filesystem import RawPayloadFileStore
from books_of_time.task_orchestrator.discovery import DiscoveryScheduler
from books_of_time.task_orchestrator.discovery_loop import DiscoveryLoop

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bot")
    parser.add_argument("--config", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")

    monitor = subparsers.add_parser("monitor-video")
    monitor.add_argument("bvid")
    monitor.add_argument("--priority", type=int, default=100)

    video = subparsers.add_parser("video")
    video_sub = video.add_subparsers(dest="video_command", required=True)
    comments = video_sub.add_parser("comments")
    comments.add_argument("bvid")
    comments.add_argument("--mode", choices=["hot"], default="hot")
    comments.add_argument("--priority", type=int, default=80)

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

    discover = subparsers.add_parser("discover-user")
    discover.add_argument("mid")
    discover.add_argument("--page", type=int, default=1)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    if args.command == "init-db":
        await create_schema(args.config)
        logger.info("Database schema is ready")
        return

    cfg = load_config(args.config)

    if args.command == "monitor-video":
        await _monitor_video(cfg, args.bvid, args.priority)
        return

    if args.command == "video" and args.video_command == "comments":
        await _enqueue_video_comments(cfg, args.bvid, args.mode, args.priority)
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
) -> None:
    if mode != "hot":
        raise ValueError(f"Unsupported comment mode: {mode}")

    session_factory = build_session_factory(cfg)
    async with session_factory() as session:
        await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id=bvid,
            priority=priority,
            payload={"bvid": bvid, "mode": mode, "page": 1},
            not_before=datetime.now(UTC),
            idempotency_key=f"{TaskKind.FETCH_HOT_COMMENTS.value}:video:{bvid}:hot",
        )
        await session.commit()
    logger.info("Queued hot comments task for %s", bvid)


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

    raw_dir = Path(cfg.get("storage", {}).get("raw_dir", "./data/raw"))
    body = RawPayloadFileStore(raw_dir).read_uri(raw.storage_uri)
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
    matrix_uids = [str(uid) for uid in discovery_cfg.get("matrix_uids", [])]
    effective_interval = (
        float(interval_seconds)
        if interval_seconds is not None
        else float(scheduler_cfg.get("discovery_scan_seconds", 60))
    )
    loop = DiscoveryLoop(
        session_factory=build_session_factory(cfg),
        client=build_bilibili_client(cfg),
        matrix_uids=matrix_uids,
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
