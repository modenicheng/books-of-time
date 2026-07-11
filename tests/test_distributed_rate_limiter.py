import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from books_of_time.app import build_rate_limiter
from books_of_time.db.base import Base
from books_of_time.db.models import RequestBudgetState
from books_of_time.http.rate_limiter import (
    DatabaseTokenBucketRateLimiter,
    RateLimitRule,
    TokenBucketRateLimiter,
)


@pytest.mark.asyncio
async def test_database_limiters_share_one_bucket_across_instances() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    rules = {"global": RateLimitRule(rps=0.001, burst=2)}
    first = DatabaseTokenBucketRateLimiter(factory, rules)
    second = DatabaseTokenBucketRateLimiter(factory, rules)

    await first.acquire("global")
    await second.acquire("global")

    async with factory() as session:
        state = await session.get(RequestBudgetState, "global")
    assert state is not None
    assert state.tokens < 0.1
    assert state.refill_rate == 0.001
    assert state.burst == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_database_limiter_reserves_all_budget_keys_atomically() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def reject_wait(_seconds: float) -> None:
        raise RuntimeError("would wait")

    limiter = DatabaseTokenBucketRateLimiter(
        factory,
        {
            "global": RateLimitRule(rps=0.001, burst=2),
            "request": RateLimitRule(rps=0.001, burst=1),
        },
        sleep=reject_wait,
    )
    await limiter.acquire("request")

    with pytest.raises(RuntimeError, match="would wait"):
        await limiter.acquire_many(("global", "request"))

    async with factory() as session:
        global_state = await session.get(RequestBudgetState, "global")
        request_state = await session.get(RequestBudgetState, "request")
    assert global_state is not None
    assert global_state.tokens == 2
    assert request_state is not None
    assert request_state.tokens < 0.1
    await engine.dispose()


@pytest.mark.asyncio
async def test_database_limiter_rejects_rule_mismatch() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await DatabaseTokenBucketRateLimiter(
        factory,
        {"global": RateLimitRule(rps=1, burst=1)},
    ).acquire("global")

    with pytest.raises(RuntimeError, match="rule mismatch"):
        await DatabaseTokenBucketRateLimiter(
            factory,
            {"global": RateLimitRule(rps=2, burst=1)},
        ).acquire("global")
    await engine.dispose()


def test_rate_limiter_builder_selects_coordination_by_database() -> None:
    rules = {"global": {"rps": 1, "burst": 1}}
    sqlite = build_rate_limiter(
        {
            "database": {"url": "sqlite+aiosqlite:///:memory:"},
            "rate_limit": rules,
        }
    )
    postgres = build_rate_limiter(
        {
            "database": {"url": "postgresql+asyncpg://localhost/books"},
            "rate_limit": rules,
        }
    )
    login = build_rate_limiter(
        {
            "database": {"url": "postgresql+asyncpg://localhost/books"},
            "rate_limit": rules,
        },
        distributed=False,
    )

    assert isinstance(sqlite, TokenBucketRateLimiter)
    assert isinstance(postgres, DatabaseTokenBucketRateLimiter)
    assert isinstance(login, TokenBucketRateLimiter)
