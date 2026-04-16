from redis.asyncio import Redis, from_url

from src.config import settings

_redis: Redis | None = None


async def get_redis_pool() -> Redis:
    global _redis
    if _redis is None:
        _redis = from_url(settings.redis_url, decode_responses=True)
    return _redis


async def close_redis_pool() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
