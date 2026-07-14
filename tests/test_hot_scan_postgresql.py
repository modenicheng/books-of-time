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
from books_of_time.db.comment_scan_repositories import (
    CommentScanRunRepository,
    HotScanRunPlan,
)
from books_of_time.db.models import (
    CollectionPolicyVersion,
    CollectionTask,
    KnownVideo,
)
from books_of_time.db.repositories import CollectionTaskRepository
from books_of_time.domain.enums import CommentScanMode, TaskKind


@pytest.mark.asyncio
async def test_two_sessions_resolve_one_scan_slice_in_isolated_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("BOT_TEST_POSTGRESQL_URL")
    if not database_url:
        pytest.skip("BOT_TEST_POSTGRESQL_URL is not configured")

    schema = f"bot_test_hot_scan_{uuid4().hex}"
    admin_engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    monkeypatch.setenv("BOT_DATABASE_SCHEMA", schema)
    config_path = tmp_path / "postgresql-hot-scan.yaml"
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
                        bvid="BV-PG-RACE",
                        source_mid="42",
                        pubdate=now - timedelta(hours=1),
                        first_seen_at=now - timedelta(hours=1),
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            await session.flush()
            scan, _ = await CommentScanRunRepository(session).materialize_hot(
                HotScanRunPlan(
                    scan_key="snapshot:BV-PG-RACE:hot_core",
                    bvid="BV-PG-RACE",
                    snapshot_cohort_id=None,
                    mode=CommentScanMode.HOT_CORE,
                    target_pages=3,
                    start_page=1,
                    end_page=3,
                    policy_version="cohort-default-v2",
                    extra={},
                ),
                now=now,
            )
            scan_id = scan.id

        async def enqueue_slice() -> tuple[int, int]:
            async with session_factory.begin() as session:
                task = await CollectionTaskRepository(session).enqueue(
                    kind=TaskKind.FETCH_HOT_COMMENTS,
                    target_type="video",
                    target_id="BV-PG-RACE",
                    priority=100,
                    payload={"page": 1},
                    not_before=now,
                    idempotency_key="snapshot:BV-PG-RACE:hot_core:active:0",
                    comment_scan_run_id=scan_id,
                    scan_slice_no=0,
                    scan_slice_key=f"{scan_id}:hot_core:0",
                )
                visible_count = await session.scalar(
                    select(func.count(CollectionTask.id))
                )
                return task.id, int(visible_count or 0)

        first, second = await asyncio.wait_for(
            asyncio.gather(enqueue_slice(), enqueue_slice()),
            timeout=15,
        )
        async with session_factory() as session:
            final_count = await session.scalar(select(func.count(CollectionTask.id)))

        assert first[0] == second[0]
        assert first[1] == 1
        assert second[1] == 1
        assert final_count == 1
        await engine.dispose()
    finally:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
