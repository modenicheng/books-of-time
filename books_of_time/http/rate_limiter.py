from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True)
class RateLimitRule:
    rps: float  # token refill rate (tokens/sec)
    burst: int  # max bucket capacity


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
