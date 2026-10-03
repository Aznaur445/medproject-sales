"""Fixed-window attempt counters in Redis (login brute-force protection)."""

from redis.asyncio import Redis


class AttemptLimiter:
    def __init__(self, redis: Redis, prefix: str, max_attempts: int, window_seconds: int) -> None:
        self.redis = redis
        self.prefix = prefix
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds

    def _key(self, subject: str) -> str:
        return f"rl:{self.prefix}:{subject}"

    async def is_blocked(self, *subjects: str) -> bool:
        for subject in subjects:
            value = await self.redis.get(self._key(subject))
            if value is not None and int(value) >= self.max_attempts:
                return True
        return False

    async def register_failure(self, *subjects: str) -> None:
        for subject in subjects:
            key = self._key(subject)
            count = await self.redis.incr(key)
            if count == 1:
                await self.redis.expire(key, self.window_seconds)

    async def reset(self, *subjects: str) -> None:
        await self.redis.delete(*(self._key(s) for s in subjects))
