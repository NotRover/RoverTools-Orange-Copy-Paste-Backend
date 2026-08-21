"""What a request is allowed to do when Redis will not answer.

The symptom this covers: `GET /api/v1/spaces` returning 500 on
`redis.exceptions.ConnectionError`. Every field on that response comes from
Postgres except one presence hint, so a presence store that is down must not
cost the caller the list.

Two layers, tested separately:

* the pool is configured so a connection that went stale while the service was
  idle is reconnected instead of raising;
* the handful of call sites that only *display* Redis state degrade, while the
  ones whose answer drives a decision keep raising.

No database and no Redis: these call the units directly.
"""

import asyncio

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src import realtime as rt
from src.redis_client import client_kwargs


_DOWN = "Error 111 connecting to redis:6379"


class _DeadPipeline:
    def smembers(self, key: str):
        return self

    def exists(self, key: str):
        return self

    async def execute(self):
        raise RedisConnectionError(_DOWN)


class _DeadRedis:
    """Every operation fails the way an unreachable Redis fails."""

    async def smembers(self, key: str):
        raise RedisConnectionError(_DOWN)

    async def exists(self, key: str):
        raise RedisConnectionError(_DOWN)

    async def publish(self, channel: str, message: str):
        raise RedisConnectionError(_DOWN)

    def pipeline(self, transaction: bool = True):
        return _DeadPipeline()


# ── Layer 1: the pool ────────────────────────────────────────────────


def test_the_request_pool_is_configured_for_a_stale_connection():
    """`from_url` builds the pool itself, so it inherits none of `Redis.__init__`'s
    defaults: without these it gets zero retries and no health check, and the
    first request after an idle spell raises."""
    kwargs = client_kwargs(blocking_reads=False)
    assert kwargs["health_check_interval"] == 30
    assert kwargs["socket_connect_timeout"] == 5
    assert kwargs["socket_timeout"] == 5
    assert kwargs["socket_keepalive"] is True
    assert kwargs["retry"].get_retries() == 3
    assert RedisConnectionError in kwargs["retry_on_error"]


def test_the_listener_client_has_no_read_timeout():
    """The pub/sub listener blocks inside a read on purpose. `read_response`
    falls back to `socket_timeout` when given no explicit timeout, so setting one
    would tear the subscription down on every quiet interval. Keepalive is what
    detects a dead peer there."""
    kwargs = client_kwargs(blocking_reads=True)
    assert "socket_timeout" not in kwargs
    assert kwargs["socket_keepalive"] is True
    assert kwargs["health_check_interval"] == 30


# ── Layer 2: what degrades, and what must not ────────────────────────


def test_a_presence_snapshot_reports_offline_rather_than_failing():
    """The single change that stops the spaces list 500ing."""
    assert asyncio.run(rt.presence_for_users(_DeadRedis(), ["u", "v"])) == set()


def test_a_presence_snapshot_is_two_round_trips_whatever_the_member_count():
    """The per-member form multiplied the read timeout by the size of the space.

    Counted against fakeredis rather than asserted in prose, because the pipeline
    is the whole point: one member with three devices and one with none must
    still be two `execute` calls.
    """
    from fakeredis.aioredis import FakeRedis

    async def scenario() -> set[str]:
        redis = FakeRedis(decode_responses=True)
        calls = 0
        real_pipeline = redis.pipeline

        def counting_pipeline(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_pipeline(*args, **kwargs)

        redis.pipeline = counting_pipeline
        for device in ("d1", "d2", "d3"):
            await redis.sadd(rt.devices_set_key("live"), device)
        # Only one of the three is heartbeating; membership alone is not presence.
        await redis.set(rt.presence_key("live", "d2"), "1", ex=rt.PRESENCE_TTL)
        # A member with a device set but no live key, and one with no set at all.
        await redis.sadd(rt.devices_set_key("stale"), "d9")
        online = await rt.presence_for_users(redis, ["live", "stale", "never-seen"])
        assert calls == 2, calls
        await redis.aclose()
        return online

    assert asyncio.run(scenario()) == {"live"}


def test_the_deciding_presence_check_still_raises():
    """`user_is_online` drives two decisions - publishing user-offline on socket
    teardown, and the sweeper evicting a device. Failing to False there announces
    that a user with live devices went away, and nothing re-announces them, so
    the guard must stay out of this one."""
    with pytest.raises(RedisConnectionError):
        asyncio.run(rt.user_is_online(_DeadRedis(), "u"))


def test_a_dropped_fan_out_is_counted_and_not_raised():
    """Every `publish_*` helper funnels through `publish`, and every one is called
    after its write is already committed. Raising here answers 500 for work that
    succeeded."""
    before = rt.dropped_events()
    asyncio.run(rt.publish(_DeadRedis(), "user:u", "sync:entry", {"client_id": "c"}))
    assert rt.dropped_events() == before + 1
