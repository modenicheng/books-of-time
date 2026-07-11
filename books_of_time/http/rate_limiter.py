from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from books_of_time.db.models import RequestBudgetState


@dataclass(frozen=True)
class RateLimitRule:
    rps: float  # token refill rate (tokens/sec)
    burst: int  # max bucket capacity

    def __post_init__(self) -> None:
        if self.rps <= 0:
            raise ValueError("rate limit rps must be greater than zero")
        if self.burst < 1:
            raise ValueError("rate limit burst must be at least one")


class RateLimiter(Protocol):
    async def acquire(self, key: str) -> None: ...

    async def acquire_many(self, keys: tuple[str, ...]) -> None: ...


async def acquire_rate_limits(
    limiter: RateLimiter | None,
    keys: tuple[str, ...],
) -> None:
    if limiter is None:
        return
    acquire_many = getattr(limiter, "acquire_many", None)
    if acquire_many is not None:
        await acquire_many(keys)
        return
    for key in keys:
        await limiter.acquire(key)


class TokenBucketRateLimiter:
    """Token bucket rate limiter with per-key isolation."""

    def __init__(self, rules: dict[str, RateLimitRule]) -> None:
        self.rules = rules
        # per-key bucket state
        self._tokens = {key: float(rule.burst) for key, rule in rules.items()}
        self._last_seen = {key: monotonic() for key in rules}
        self._locks = {key: asyncio.Lock() for key in rules}

    async def acquire(self, key: str) -> None:
        """Acquire one token, blocking until available."""
        rule = self.rules.get(key)
        if rule is None:
            return

        async with self._locks[key]:
            while True:
                now = monotonic()
                elapsed = now - self._last_seen[key]
                # refill tokens proportionally, capped at burst
                self._tokens[key] = min(
                    float(rule.burst),
                    self._tokens[key] + (elapsed * rule.rps),
                )
                self._last_seen[key] = now

                if self._tokens[key] >= 1:
                    self._tokens[key] -= 1
                    return

                # not enough tokens, wait for the next one
                missing = 1 - self._tokens[key]
                await asyncio.sleep(missing / rule.rps)

    async def acquire_many(self, keys: tuple[str, ...]) -> None:
        for key in keys:
            await self.acquire(key)


class DatabaseTokenBucketRateLimiter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        rules: dict[str, RateLimitRule],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.session_factory = session_factory
        self.rules = rules
        self.sleep = sleep

    async def acquire(self, key: str) -> None:
        await self.acquire_many((key,))

    async def acquire_many(self, keys: tuple[str, ...]) -> None:
        effective_keys = tuple(sorted({key for key in keys if key in self.rules}))
        if not effective_keys:
            return
        while True:
            wait_seconds = await self._acquire_or_wait(effective_keys)
            if wait_seconds <= 0:
                return
            await self.sleep(wait_seconds)

    async def _acquire_or_wait(self, keys: tuple[str, ...]) -> float:
        async with self.session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                for key in keys:
                    await _ensure_budget_state(
                        session,
                        key=key,
                        rule=self.rules[key],
                        now=now,
                    )
                states = list(
                    await session.scalars(
                        select(RequestBudgetState)
                        .where(RequestBudgetState.budget_key.in_(keys))
                        .order_by(RequestBudgetState.budget_key)
                        .with_for_update()
                    )
                )
                states_by_key = {state.budget_key: state for state in states}
                missing_keys = set(keys) - states_by_key.keys()
                if missing_keys:
                    missing = ", ".join(sorted(missing_keys))
                    raise RuntimeError(f"Request budget states disappeared: {missing}")

                available_by_key: dict[str, float] = {}
                wait_seconds = 0.0
                for key in keys:
                    state = states_by_key[key]
                    rule = self.rules[key]
                    _validate_rule(key, state, rule)
                    elapsed = max(
                        (now - state.last_refill_at).total_seconds(),
                        0.0,
                    )
                    available = min(
                        float(rule.burst),
                        state.tokens + (elapsed * rule.rps),
                    )
                    available_by_key[key] = available
                    if available < 1:
                        wait_seconds = max(
                            wait_seconds,
                            (1 - available) / rule.rps,
                        )
                    state.tokens = available
                    state.last_refill_at = now
                    state.updated_at = now

                if wait_seconds > 0:
                    return wait_seconds
                for key in keys:
                    states_by_key[key].tokens = available_by_key[key] - 1
                return 0.0


def _validate_rule(
    key: str,
    state: RequestBudgetState,
    rule: RateLimitRule,
) -> None:
    if not math.isclose(state.refill_rate, rule.rps) or state.burst != rule.burst:
        raise RuntimeError(
            f"Request budget rule mismatch for {key}: database has "
            f"rps={state.refill_rate}, burst={state.burst}; process has "
            f"rps={rule.rps}, burst={rule.burst}"
        )


async def _database_now(session: AsyncSession) -> datetime:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        value = await session.scalar(select(func.clock_timestamp()))
        if value is None:
            raise RuntimeError("Database did not return clock_timestamp()")
        return value.astimezone(UTC)
    return datetime.now(UTC)


async def _ensure_budget_state(
    session: AsyncSession,
    *,
    key: str,
    rule: RateLimitRule,
    now: datetime,
) -> None:
    values = {
        "budget_key": key,
        "tokens": float(rule.burst),
        "refill_rate": rule.rps,
        "burst": rule.burst,
        "last_refill_at": now,
        "created_at": now,
        "updated_at": now,
    }
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        statement = postgresql_insert(RequestBudgetState)
    elif dialect_name == "sqlite":
        statement = sqlite_insert(RequestBudgetState)
    else:
        raise ValueError(f"Unsupported request budget dialect: {dialect_name}")
    await session.execute(statement.values(**values).on_conflict_do_nothing())
