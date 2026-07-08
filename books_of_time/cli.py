from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from books_of_time.app import build_bilibili_client, build_session_factory, build_worker
from books_of_time.common.logger import get_logger
from books_of_time.config import load_config
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.db.schema import create_schema
from books_of_time.domain.enums import TaskKind
from books_of_time.parsers.discovery import parse_user_video_list
from books_of_time.task_orchestrator.discovery import DiscoveryScheduler

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bot")
    parser.add_argument("--config", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")

    monitor = subparsers.add_parser("monitor-video")
    monitor.add_argument("bvid")
    monitor.add_argument("--priority", type=int, default=100)

    worker = subparsers.add_parser("worker")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    worker_sub.add_parser("run-once")

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

    if args.command == "worker" and args.worker_command == "run-once":
        worker = build_worker(
            cfg,
            run_id=f"cli-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}",
            lease_owner="cli-worker",
        )
        executed = await worker.run_once()
        logger.info("Worker executed task: %s", executed)
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
        )
        await session.commit()
    logger.info("Queued video stats task for %s", bvid)


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
