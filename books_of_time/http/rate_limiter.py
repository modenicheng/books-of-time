from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True)
class RateLimitRule:
    rps: float
    burst: int


class TokenBucketRateLimiter:
    def __init__(self, rules: dict[str, RateLimitRule]) -> None:
        self.rules = rules
        self._tokens = {key: float(rule.burst) for key, rule in rules.items()}
        self._last_seen = {key: monotonic() for key in rules}
        self._locks = {key: asyncio.Lock() for key in rules}

    async def acquire(self, key: str) -> None:
        rule = self.rules.get(key)
        if rule is None:
            return

        async with self._locks[key]:
            while True:
                now = monotonic()
                elapsed = now - self._last_seen[key]
                self._tokens[key] = min(
                    float(rule.burst),
                    self._tokens[key] + (elapsed * rule.rps),
                )
                self._last_seen[key] = now

                if self._tokens[key] >= 1:
                    self._tokens[key] -= 1
                    return

                missing = 1 - self._tokens[key]
                await asyncio.sleep(missing / rule.rps)
