from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from books_of_time.db.latest_scan_repositories import (
    LatestScanRunPlan,
    LatestScanRunRepository,
)
from books_of_time.db.models import (
    CollectionPolicyVersion,
    CommentScanRun,
    FrontierState,
    KnownVideo,
)
from books_of_time.db.repositories import FrontierStateRepository
from books_of_time.domain.enums import CommentScanMode


@pytest.mark.asyncio
async def test_two_sessions_claim_one_latest_scan_in_isolated_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("BOT_TEST_POSTGRESQL_URL")
    if not database_url:
        pytest.skip("BOT_TEST_POSTGRESQL_URL is not configured")

    schema = f"bot_test_latest_scan_{uuid4().hex}"
    admin_engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    monkeypatch.setenv("BOT_DATABASE_SCHEMA", schema)
    config_path = tmp_path / "postgresql-latest-scan.yaml"
    config_path.write_text(
        f"database:\n  url: {json.dumps(database_url)}\n",
        encoding="utf-8",
    )
    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_config.attributes["bot_config_path"] = str(config_path)
    alembic_config.attributes["skip_logger_config"] = True

    try:
        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": schema}},
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

        async with session_factory.begin() as session:
            session.add_all(
                [
                    CollectionPolicyVersion(
                        version="cohort-default-v2",
                        policy_kind="snapshot_cohort",
                        scope_type="global",
                        scope_id="global",
                        timezone="Asia/Shanghai",
                        policy={},
                        algorithm="configured-fixed-v1",
                        created_at=now,
                        activated_at=now,
                        active=True,
                    ),
                    KnownVideo(
                        bvid="BV-PG-LATEST-RACE",
                        source_mid="42",
                        pubdate=now - timedelta(hours=1),
                        first_seen_at=now - timedelta(hours=1),
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )

        async def create_frontier() -> tuple[int, int]:
            async with session_factory.begin() as session:
                state = await FrontierStateRepository(session).get_or_create(
                    target_type="video",
                    target_id="BV-PG-LATEST-RACE",
                    frontier_type="latest_comments",
                    now=now,
                )
                count = await session.scalar(select(func.count(FrontierState.id)))
                return state.id, int(count or 0)

        first_frontier, second_frontier = await asyncio.wait_for(
            asyncio.gather(create_frontier(), create_frontier()),
            timeout=15,
        )
        assert first_frontier[0] == second_frontier[0]
        assert first_frontier[1] == 1
        assert second_frontier[1] == 1

        ready = 0
        ready_lock = asyncio.Lock()
        start = asyncio.Event()

        async def claim(scan_key: str) -> tuple[int, bool, int, int]:
            nonlocal ready
            async with session_factory.begin() as session:
                state = await FrontierStateRepository(session).get_or_create(
                    target_type="video",
                    target_id="BV-PG-LATEST-RACE",
                    frontier_type="latest_comments",
                    now=now,
                )
                async with ready_lock:
                    ready += 1
                    if ready == 2:
                        start.set()
                await start.wait()
                result = await LatestScanRunRepository(session).claim_or_join(
                    LatestScanRunPlan(
                        scan_key=scan_key,
                        bvid="BV-PG-LATEST-RACE",
                        snapshot_cohort_id=None,
                        parent_scan_run_id=None,
                        mode=CommentScanMode.BASELINE_TAIL,
                        policy_version="cohort-default-v2",
                        reason="routine",
                        start_frontier_rpid=None,
                        start_anchor_set=[],
                        start_cursor=None,
                        extra={},
                    ),
                    frontier_state=state,
                    expected_version=state.version,
                    now=now,
                )
                visible_count = await session.scalar(
                    select(func.count(CommentScanRun.id))
                )
                return (
                    result.scan.id,
                    result.created,
                    result.frontier_state.version,
                    int(visible_count or 0),
                )

        first, second = await asyncio.wait_for(
            asyncio.gather(
                claim("snapshot:BV-PG-LATEST-RACE:latest:a"),
                claim("snapshot:BV-PG-LATEST-RACE:latest:b"),
            ),
            timeout=15,
        )
        async with session_factory() as session:
            final_count = await session.scalar(select(func.count(CommentScanRun.id)))
            final_state = await session.scalar(select(FrontierState))

        assert first[0] == second[0]
        assert {first[1], second[1]} == {True, False}
        assert first[2] == second[2] == 1
        assert first[3] == second[3] == 1
        assert final_count == 1
        assert final_state is not None
        assert final_state.active_scan_run_id == first[0]
        assert final_state.version == 1
        await engine.dispose()
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
