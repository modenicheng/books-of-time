import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time import cli
from books_of_time.cli import _show_coverage, build_parser
from books_of_time.coverage import CoverageDraft
from books_of_time.db.base import Base
from books_of_time.db.models import ScheduledJob, ServiceInstance, VideoMetricSnapshot
from books_of_time.db.repositories import (
    CollectionCoverageRepository,
    CollectionTaskRepository,
    RawPayloadRepository,
)
from books_of_time.domain.enums import BilibiliRequestType, TaskKind, TaskStatus
from books_of_time.http.client import FetchResult
from books_of_time.storage.filesystem import RawPayloadFileStore


def test_collect_latest_comments_parser_defaults() -> None:
    args = build_parser().parse_args(["collect-latest-comments", "BV1abc"])

    assert args.command == "collect-latest-comments"
    assert args.bvid == "BV1abc"
    assert args.priority == 70
    assert args.max_scan_seconds == 55


def test_collect_latest_comments_parser_accepts_overrides() -> None:
    args = build_parser().parse_args(
        [
            "collect-latest-comments",
            "BV1abc",
            "--priority",
            "90",
            "--max-scan-seconds",
            "12",
        ]
    )

    assert args.priority == 90
    assert args.max_scan_seconds == 12


def test_service_parser_supports_runtime_and_operations_commands() -> None:
    run_args = build_parser().parse_args(
        ["service", "run", "--max-worker-iterations", "1"]
    )
    assert run_args.service_command == "run"
    assert run_args.max_worker_iterations == 1

    doctor_args = build_parser().parse_args(["service", "doctor"])
    assert doctor_args.service_command == "doctor"

    health_args = build_parser().parse_args(["service", "health"])
    assert health_args.service_command == "health"

    status_args = build_parser().parse_args(["service", "status", "--limit", "5"])
    assert status_args.service_command == "status"
    assert status_args.limit == 5


@pytest.mark.asyncio
async def test_run_dispatches_service_commands(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    async def fake_run_service(cfg, *, max_worker_iterations) -> None:
        calls.append(("run", max_worker_iterations))

    async def fake_doctor(cfg) -> None:
        calls.append(("doctor", cfg))

    async def fake_health(cfg) -> None:
        calls.append(("health", cfg))

    async def fake_status(cfg, *, limit) -> None:
        calls.append(("status", limit))

    cfg = {"database": {"url": "unused"}}
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "_run_service", fake_run_service)
    monkeypatch.setattr(cli, "_show_service_doctor", fake_doctor)
    monkeypatch.setattr(cli, "_show_service_health", fake_health)
    monkeypatch.setattr(cli, "_show_service_status", fake_status)

    await cli._run(
        build_parser().parse_args(["service", "run", "--max-worker-iterations", "2"])
    )
    await cli._run(build_parser().parse_args(["service", "doctor"]))
    await cli._run(build_parser().parse_args(["service", "health"]))
    await cli._run(build_parser().parse_args(["service", "status", "--limit", "7"]))

    assert calls == [
        ("run", 2),
        ("doctor", cfg),
        ("health", cfg),
        ("status", 7),
    ]


@pytest.mark.asyncio
async def test_run_service_finishes_finite_sqlite_smoke(tmp_path) -> None:
    db_path = tmp_path / "service.sqlite3"
    cfg = {
        "database": {"url": f"sqlite+aiosqlite:///{db_path}"},
        "storage": {
            "raw_dir": str(tmp_path / "raw"),
            "media_dir": str(tmp_path / "media"),
        },
        "service": {
            "roles": ["worker", "scheduler"],
            "worker_idle_sleep_seconds": 0,
            "heartbeat_seconds": 0.01,
            "shutdown_grace_seconds": 1,
        },
    }
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    await cli._run_service(cfg, max_worker_iterations=1)

    engine = create_async_engine(cfg["database"]["url"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        instances = list(await session.scalars(select(ServiceInstance)))
        scheduled_jobs = list(await session.scalars(select(ScheduledJob)))
    await engine.dispose()

    assert len(instances) == 1
    assert instances[0].status == "stopped"
    assert len(scheduled_jobs) == 3


def test_coverage_parser_accepts_bvid() -> None:
    args = build_parser().parse_args(["coverage", "BV1abc"])

    assert args.command == "coverage"
    assert args.bvid == "BV1abc"
    assert args.limit == 20


def test_worker_loop_parser_accepts_options() -> None:
    args = build_parser().parse_args(
        [
            "worker",
            "loop",
            "--idle-sleep-seconds",
            "0.25",
            "--max-iterations",
            "2",
            "--stop-when-idle",
        ]
    )

    assert args.command == "worker"
    assert args.worker_command == "loop"
    assert args.idle_sleep_seconds == 0.25
    assert args.max_iterations == 2
    assert args.stop_when_idle is True


def test_task_list_and_retry_failed_parsers() -> None:
    list_args = build_parser().parse_args(["task", "list", "--status", "failed"])
    retry_args = build_parser().parse_args(
        [
            "task",
            "retry-failed",
            "--target-id",
            "BV1abc",
            "--kind",
            "fetch_latest_comments",
        ]
    )

    assert list_args.command == "task"
    assert list_args.task_command == "list"
    assert list_args.status == "failed"
    assert retry_args.command == "task"
    assert retry_args.task_command == "retry-failed"
    assert retry_args.target_id == "BV1abc"
    assert retry_args.kind == "fetch_latest_comments"


def test_raw_inspect_parser_accepts_payload_id() -> None:
    args = build_parser().parse_args(["raw", "inspect", "123", "--preview-bytes", "20"])

    assert args.command == "raw"
    assert args.raw_command == "inspect"
    assert args.raw_payload_id == 123
    assert args.preview_bytes == 20


def test_video_stats_parser_accepts_bvid_and_limit() -> None:
    args = build_parser().parse_args(["video", "stats", "BV1abc", "--limit", "5"])

    assert args.command == "video"
    assert args.video_command == "stats"
    assert args.bvid == "BV1abc"
    assert args.limit == 5


def test_video_comments_parser_accepts_tier_and_page_limit() -> None:
    args = build_parser().parse_args(
        [
            "video",
            "comments",
            "BV1abc",
            "--mode",
            "hot",
            "--tier",
            "a",
            "--page-limit",
            "7",
        ]
    )

    assert args.command == "video"
    assert args.video_command == "comments"
    assert args.bvid == "BV1abc"
    assert args.tier == "a"
    assert args.page_limit == 7


def test_discovery_loop_parser_accepts_options() -> None:
    args = build_parser().parse_args(
        [
            "discovery",
            "loop",
            "--interval-seconds",
            "0.1",
            "--max-iterations",
            "1",
            "--stop-when-idle",
        ]
    )

    assert args.command == "discovery"
    assert args.discovery_command == "loop"
    assert args.interval_seconds == 0.1
    assert args.max_iterations == 1
    assert args.stop_when_idle is True


def test_resolve_discovery_uid_sources_includes_game_and_event_pools() -> None:
    sources = cli._resolve_discovery_uid_sources(
        {
            "matrix_uids": [100, "200"],
            "game_uid_pools": {
                "genshin": [300],
                "hsr": {"uids": ["400"]},
            },
            "event_uid_pools": {
                "version_42": {"uids": [500]},
            },
        }
    )

    assert [(source.mid, source.pool_type, source.pool_id) for source in sources] == [
        ("100", "matrix", None),
        ("200", "matrix", None),
        ("300", "game", "genshin"),
        ("400", "game", "hsr"),
        ("500", "event", "version_42"),
    ]


@pytest.mark.asyncio
async def test_show_coverage_lists_latest_rows(tmp_path, caplog) -> None:
    db_path = tmp_path / "coverage.sqlite3"
    cfg = {"database": {"url": f"sqlite+aiosqlite:///{db_path}"}}
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2099, 1, 1, tzinfo=UTC)
    async with session_factory() as session:
        task = await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type="video",
            target_id="BV1abc",
            priority=70,
            payload={"bvid": "BV1abc"},
            not_before=now,
        )
        await CollectionCoverageRepository(session).insert_from_draft(
            task=task,
            run_id="run-1",
            draft=CoverageDraft(
                task_kind=TaskKind.FETCH_LATEST_COMMENTS,
                target_type="video",
                target_id="BV1abc",
                pages_requested=2,
                pages_succeeded=2,
                items_observed=2,
                reason="frontier_reached",
            ),
            started_at=now,
            finished_at=now,
        )
        await session.commit()
    await engine.dispose()

    await _show_coverage(cfg, "BV1abc", 20)

    assert "fetch_latest_comments" in caplog.text
    assert "status=succeeded" in caplog.text
    assert "reason=frontier_reached" in caplog.text
    assert "pages=2/2" in caplog.text


@pytest.mark.asyncio
async def test_inspect_raw_payload_logs_metadata_and_preview(tmp_path, caplog) -> None:
    db_path = tmp_path / "raw.sqlite3"
    raw_dir = tmp_path / "raw"
    cfg = {
        "database": {"url": f"sqlite+aiosqlite:///{db_path}"},
        "storage": {"raw_dir": str(raw_dir)},
    }
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    body = b'{"message":"hello raw inspect"}'
    captured_at = datetime(2099, 1, 1, tzinfo=UTC)
    stored = RawPayloadFileStore(raw_dir).save(
        body=body,
        captured_at=captured_at,
        run_id="run-1",
        suffix=".json",
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        raw = await RawPayloadRepository(session).insert_from_fetch_result(
            result=FetchResult(
                request_type=BilibiliRequestType.VIDEO_STATS,
                method="GET",
                url="https://api.bilibili.com/x/web-interface/view",
                params={"bvid": "BV1"},
                status_code=200,
                body=body,
                captured_at=captured_at,
                response_headers={},
            ),
            stored=stored,
            parser_version="test",
        )
        raw_id = raw.id
        await session.commit()
    await engine.dispose()

    await cli._inspect_raw_payload(cfg, raw_id, preview_bytes=30)

    assert f"raw id={raw_id}" in caplog.text
    assert "bilibili:video_stats" in caplog.text
    assert "hello raw" in caplog.text


@pytest.mark.asyncio
async def test_show_video_stats_logs_latest_snapshots(tmp_path, caplog) -> None:
    db_path = tmp_path / "video-stats.sqlite3"
    cfg = {"database": {"url": f"sqlite+aiosqlite:///{db_path}"}}
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    captured_at = datetime(2099, 1, 1, tzinfo=UTC)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            VideoMetricSnapshot(
                bvid="BV1abc",
                captured_at=captured_at,
                view_count=100,
                like_count=10,
                coin_count=2,
                favorite_count=3,
                share_count=4,
                reply_count=5,
                danmaku_count=6,
                raw_payload_id=42,
            )
        )
        await session.commit()
    await engine.dispose()

    await cli._show_video_stats(cfg, "BV1abc", limit=20)

    assert "BV1abc" in caplog.text
    assert "view=100" in caplog.text
    assert "like=10" in caplog.text
    assert "raw_payload_id=42" in caplog.text


@pytest.mark.asyncio
async def test_show_video_stats_logs_empty_state(tmp_path, caplog) -> None:
    db_path = tmp_path / "video-stats-empty.sqlite3"
    cfg = {"database": {"url": f"sqlite+aiosqlite:///{db_path}"}}
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    await cli._show_video_stats(cfg, "BVEMPTY", limit=20)

    assert "No video stats snapshots for BVEMPTY" in caplog.text


@pytest.mark.asyncio
async def test_run_discovery_loop_uses_configured_matrix_uids(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "discovery.sqlite3"
    cfg = {
        "database": {"url": f"sqlite+aiosqlite:///{db_path}"},
        "discovery": {"matrix_uids": ["123"]},
        "scheduler": {"discovery_scan_seconds": 60},
    }
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    now = datetime(2099, 1, 1, tzinfo=UTC)

    class FakeClient:
        async def get_user_video_list(self, mid: str, page: int = 1) -> FetchResult:
            assert mid == "123"
            assert page == 1
            body = {
                "data": {
                    "list": {
                        "vlist": [
                            {"bvid": "BVDISCOVERY", "created": int(now.timestamp())}
                        ]
                    }
                }
            }
            return FetchResult(
                request_type=BilibiliRequestType.USER_VIDEO_LIST,
                method="GET",
                url="https://api.bilibili.com/x/space/wbi/arc/search",
                params={"mid": mid},
                status_code=200,
                body=json.dumps(body).encode(),
                captured_at=now,
                response_headers={},
            )

    monkeypatch.setattr(cli, "build_bilibili_client", lambda cfg: FakeClient())

    await cli._run_discovery_loop(
        cfg,
        interval_seconds=0.1,
        max_iterations=1,
        stop_when_idle=False,
    )

    engine = create_async_engine(cfg["database"]["url"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tasks = await CollectionTaskRepository(session).list_tasks(limit=10)
    await engine.dispose()

    assert [task.target_id for task in tasks] == ["BVDISCOVERY"]


@pytest.mark.asyncio
async def test_list_tasks_logs_matching_tasks(tmp_path, caplog) -> None:
    db_path = tmp_path / "tasks.sqlite3"
    cfg = {"database": {"url": f"sqlite+aiosqlite:///{db_path}"}}
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2099, 1, 1, tzinfo=UTC)
    async with session_factory() as session:
        task = await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_HOT_COMMENTS,
            target_type="video",
            target_id="BVFAILED",
            priority=80,
            payload={"bvid": "BVFAILED"},
            not_before=now,
        )
        task.status = TaskStatus.FAILED
        task.retry_count = 2
        await session.commit()
    await engine.dispose()

    await cli._list_tasks(cfg, status="failed", limit=20)

    assert "BVFAILED" in caplog.text
    assert "status=failed" in caplog.text
    assert "retries=2/3" in caplog.text


@pytest.mark.asyncio
async def test_monitor_video_reuses_active_task(tmp_path) -> None:
    db_path = tmp_path / "monitor.sqlite3"
    cfg = {"database": {"url": f"sqlite+aiosqlite:///{db_path}"}}
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    await cli._monitor_video(cfg, "BVDEDUP", priority=100)
    await cli._monitor_video(cfg, "BVDEDUP", priority=50)

    engine = create_async_engine(cfg["database"]["url"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tasks = await CollectionTaskRepository(session).list_tasks(limit=10)
    await engine.dispose()

    assert [task.target_id for task in tasks] == ["BVDEDUP"]
    assert tasks[0].priority == 100


@pytest.mark.asyncio
async def test_enqueue_video_comments_uses_tier_hot_page_budget(tmp_path) -> None:
    db_path = tmp_path / "comments.sqlite3"
    cfg = {
        "database": {"url": f"sqlite+aiosqlite:///{db_path}"},
        "request_budget": {
            "a": {"hot_pages": 10},
            "c": {"hot_pages": 1},
        },
    }
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    await cli._enqueue_video_comments(
        cfg,
        "BVHOT",
        mode="hot",
        priority=80,
        tier="a",
        page_limit=None,
    )

    engine = create_async_engine(cfg["database"]["url"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tasks = await CollectionTaskRepository(session).list_tasks(limit=10)
    await engine.dispose()

    assert [task.target_id for task in tasks] == ["BVHOT"]
    assert tasks[0].payload["tier"] == "a"
    assert tasks[0].payload["page_limit"] == 10


@pytest.mark.asyncio
async def test_retry_failed_tasks_requeues_matching_tasks(tmp_path, caplog) -> None:
    db_path = tmp_path / "retry.sqlite3"
    cfg = {"database": {"url": f"sqlite+aiosqlite:///{db_path}"}}
    engine = create_async_engine(cfg["database"]["url"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2099, 1, 1, tzinfo=UTC)
    async with session_factory() as session:
        task = await CollectionTaskRepository(session).enqueue(
            kind=TaskKind.FETCH_LATEST_COMMENTS,
            target_type="video",
            target_id="BVFAILED",
            priority=70,
            payload={"bvid": "BVFAILED"},
            not_before=now,
        )
        task.status = TaskStatus.FAILED
        task.retry_count = 2
        await session.commit()

    await cli._retry_failed_tasks(
        cfg,
        target_id="BVFAILED",
        kind="fetch_latest_comments",
        limit=100,
    )

    async with session_factory() as session:
        task = await CollectionTaskRepository(session).list_tasks(
            status=TaskStatus.PENDING,
            limit=10,
        )

    await engine.dispose()

    assert [item.target_id for item in task] == ["BVFAILED"]
    assert "Retried failed tasks: 1" in caplog.text
