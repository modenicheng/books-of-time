from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.cli import _show_coverage, build_parser
from books_of_time.coverage import CoverageDraft
from books_of_time.db.base import Base
from books_of_time.db.repositories import (
    CollectionCoverageRepository,
    CollectionTaskRepository,
)
from books_of_time.domain.enums import TaskKind


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
