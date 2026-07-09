from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time import cli
from books_of_time.cli import _show_coverage, build_parser
from books_of_time.coverage import CoverageDraft
from books_of_time.db.base import Base
from books_of_time.db.repositories import (
    CollectionCoverageRepository,
    CollectionTaskRepository,
)
from books_of_time.domain.enums import TaskKind, TaskStatus


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
