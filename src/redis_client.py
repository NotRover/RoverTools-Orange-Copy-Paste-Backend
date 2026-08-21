"""The shared Redis connection pool.

`from_url` builds a `ConnectionPool` directly, so none of `Redis.__init__`'s
defaults apply to a client made this way: it gets no health check and a retry
policy of zero attempts. That is the difference between a pooled connection that
went stale while the service was idle being reconnected, and it surfacing as a
`ConnectionError` on the next request - which is what turned a presence lookup
into a 500 on the spaces list.
"""

from redis.asyncio import Redis, from_url
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

from src.config import settings

_redis: Redis | None = None

# Long enough that a busy Redis is not mistaken for a dead one, short enough that
# a dead one cannot hold a request open past the client's own patience.
_SOCKET_TIMEOUT_SECONDS = 5
_HEALTH_CHECK_INTERVAL_SECONDS = 30
_RETRIES = 3


def client_kwargs(*, blocking_reads: bool) -> dict:
    """Connection settings for one Redis client.

    `blocking_reads` is for the pub/sub listener, which spends its life inside a
    read that is *meant* to block: `read_response` falls back to `socket_timeout`
    when it is given no explicit timeout, so setting one there tears the
    subscription down on every quiet interval. TCP keepalive is what detects a
    dead peer on that client instead.
    """
    kwargs: dict = {
        "decode_responses": True,
        "health_check_interval": _HEALTH_CHECK_INTERVAL_SECONDS,
        "socket_connect_timeout": _SOCKET_TIMEOUT_SECONDS,
        "socket_keepalive": True,
        "retry": Retry(ExponentialWithJitterBackoff(base=0.05, cap=1.0), retries=_RETRIES),
        "retry_on_error": [RedisConnectionError, RedisTimeoutError],
    }
    if not blocking_reads:
        kwargs["socket_timeout"] = _SOCKET_TIMEOUT_SECONDS
    return kwargs


async def get_redis_pool() -> Redis:
    global _redis
    if _redis is None:
        _redis = from_url(settings.redis_url, **client_kwargs(blocking_reads=False))
    return _redis


async def close_redis_pool() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
