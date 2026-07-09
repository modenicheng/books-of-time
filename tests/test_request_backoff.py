from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.db.base import Base
from books_of_time.db.models import RequestBackoffState
from books_of_time.db.repositories import RequestBackoffRepository
from books_of_time.domain.enums import BilibiliRequestType
from books_of_time.http.errors import RequestErrorKind, RequestFailure


async def _create_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_request_backoff_records_and_updates_failure_state() -> None:
    engine, session_factory = await _create_session_factory()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    defaults = {"429": 10}
    try:
        async with session_factory() as session:
            repo = RequestBackoffRepository(session)
            failure = RequestFailure(
                kind=RequestErrorKind.RATE_LIMITED,
                request_type=BilibiliRequestType.COMMENT_HOT,
                message="rate limited",
                status_code=429,
            )
            first = await repo.record_failure(
                platform="bilibili",
                scope="global",
                failure=failure,
                now=now,
                default_seconds=defaults,
                max_seconds=1000,
            )
            second = await repo.record_failure(
                platform="bilibili",
                scope="global",
                failure=failure,
                now=now + timedelta(seconds=1),
                default_seconds=defaults,
                max_seconds=1000,
            )
            await session.commit()

        async with session_factory() as session:
            saved = await session.scalar(select(RequestBackoffState))
            assert first.id == second.id
            assert saved is not None
            assert saved.platform == "bilibili"
            assert saved.request_type == BilibiliRequestType.COMMENT_HOT
            assert saved.error_kind == "429"
            assert saved.status_code == 429
            assert saved.fail_count == 2
            assert saved.first_failed_at == now
            assert saved.last_failed_at == now + timedelta(seconds=1)
            assert saved.backoff_until == now + timedelta(seconds=21)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_request_backoff_uses_retry_after_before_default() -> None:
    engine, session_factory = await _create_session_factory()
    now = datetime(2099, 1, 1, tzinfo=UTC)
    try:
        async with session_factory() as session:
            state = await RequestBackoffRepository(session).record_failure(
                platform="bilibili",
                scope="global",
                failure=RequestFailure(
                    kind=RequestErrorKind.RATE_LIMITED,
                    request_type=BilibiliRequestType.DEFAULT,
                    message="retry later",
                    status_code=429,
                    retry_after_seconds=45,
                ),
                now=now,
                default_seconds={"429": 10},
                max_seconds=1000,
            )

            assert state.retry_after_seconds == 45
            assert state.backoff_until == now + timedelta(seconds=45)
    finally:
        await engine.dispose()
