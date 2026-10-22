import logging
import time
from collections import OrderedDict
from threading import Lock

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger("amrutam.security")
WINDOW_SECONDS = 60
MAX_LOCAL_KEYS = 100_000
_INCREMENT_WITH_TTL = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {count, redis.call('TTL', KEYS[1])}
"""


class RateLimitBackendError(RuntimeError):
    pass


class InMemoryRateLimiter:
    """Bounded, process-local limiter for development and tests only."""

    def __init__(self) -> None:
        self._counts: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._lock = Lock()

    async def check(self, key: str, limit: int, window_seconds: int = WINDOW_SECONDS) -> int | None:
        now = time.monotonic()
        with self._lock:
            count, expires_at = self._counts.get(key, (0, now + window_seconds))
            if expires_at <= now:
                count, expires_at = 0, now + window_seconds
            count += 1
            self._counts[key] = (count, expires_at)
            self._counts.move_to_end(key)
            while len(self._counts) > MAX_LOCAL_KEYS:
                self._counts.popitem(last=False)
            if count <= limit:
                return None
            return max(1, int(expires_at - now + 0.999))

    async def ping(self) -> bool:
        return True


class RedisRateLimiter:
    def __init__(self, client: Redis) -> None:
        self._client = client

    async def check(self, key: str, limit: int, window_seconds: int = WINDOW_SECONDS) -> int | None:
        try:
            count, ttl = await self._client.eval(_INCREMENT_WITH_TTL, 1, key, window_seconds)
        except RedisError as exc:
            raise RateLimitBackendError("Distributed rate-limit backend unavailable") from exc
        if int(count) <= limit:
            return None
        return max(1, int(ttl))

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except RedisError as exc:
            raise RateLimitBackendError("Distributed rate-limit backend unavailable") from exc

    async def close(self) -> None:
        await self._client.aclose()
