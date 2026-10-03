from functools import lru_cache

from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from app.core.config import get_settings


@lru_cache
def get_redis() -> AsyncRedis:
    return AsyncRedis.from_url(get_settings().redis_url, decode_responses=True)


@lru_cache
def get_sync_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)
