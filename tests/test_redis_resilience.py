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


# ── Presence is re-asserted, never merely extended ───────────────────


def test_a_heartbeat_restores_presence_that_was_lost():
    """The regression: the heartbeat used to call `EXPIRE`, and `EXPIRE` on a
    missing key does nothing at all.

    So any loss of the presence key was permanent for the life of the socket -
    the device kept checking in, every check was a no-op, and it read as offline
    to `GET /auth/devices` and to every member of its spaces until the app was
    restarted. Keys are lost in ordinary operation: the instance runs an LRU
    eviction policy, a restart drops everything, and a link that misses pongs for
    longer than the TTL lets the key lapse on its own.
    """
    from fakeredis.aioredis import FakeRedis

    async def scenario() -> tuple[bool, bool]:
        redis = FakeRedis(decode_responses=True)
        await rt._assert_presence(redis, "u", "d1")
        assert await rt.user_is_online(redis, "u")

        # Everything Redis held, gone - an eviction, or a restart.
        await redis.flushall()
        lost = await rt.user_is_online(redis, "u")

        # One heartbeat from the still-connected socket.
        await rt._assert_presence(redis, "u", "d1")
        restored = await rt.user_is_online(redis, "u")
        await redis.aclose()
        return lost, restored

    lost, restored = asyncio.run(scenario())
    assert lost is False, "precondition: the loss must actually be observable"
    assert restored is True, "a heartbeat has to rebuild presence, not just extend it"


def test_presence_is_asserted_in_one_round_trip():
    """Connect and every heartbeat go through here, so it is pipelined."""
    from fakeredis.aioredis import FakeRedis

    async def scenario() -> int:
        redis = FakeRedis(decode_responses=True)
        calls = 0
        real_pipeline = redis.pipeline

        def counting_pipeline(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_pipeline(*args, **kwargs)

        redis.pipeline = counting_pipeline
        await rt._assert_presence(redis, "u", "d1")
        await redis.aclose()
        return calls

    assert asyncio.run(scenario()) == 1


def test_a_departing_socket_can_ask_whether_anyone_else_is_left():
    """What the teardown path actually needs to know, asked before it removes
    itself so the answer survives a blip on the way out."""
    from fakeredis.aioredis import FakeRedis

    async def scenario() -> tuple[bool, bool]:
        redis = FakeRedis(decode_responses=True)
        await rt._assert_presence(redis, "u", "d1")
        alone = await rt.user_is_online(redis, "u", exclude_device="d1")
        await rt._assert_presence(redis, "u", "d2")
        accompanied = await rt.user_is_online(redis, "u", exclude_device="d1")
        await redis.aclose()
        return alone, accompanied

    alone, accompanied = asyncio.run(scenario())
    assert alone is False, "its own presence key must not count as company"
    assert accompanied is True


# ── Fan-out cannot be held up by one reader ──────────────────────────


class _Socket:
    """A local WebSocket, as much of one as `_broadcast_local` touches."""

    def __init__(self, *, hangs: bool = False) -> None:
        self.hangs = hangs
        self.received: list[str] = []
        self.closed = False

    async def send_text(self, raw: str) -> None:
        if self.hangs:
            await asyncio.Event().wait()  # never returns, like a stalled transport
        self.received.append(raw)

    async def close(self, code: int = 1000) -> None:
        self.closed = True


def test_one_stalled_socket_does_not_hold_up_the_others():
    """The regression: sends ran in sequence, and the listener awaits this
    function before reading the next Redis message. So a client that stopped
    reading stalled fan-out for every other socket on the replica, and then
    stalled the listener, and Redis dropped the subscriber - leaving the replica
    serving HTTP while delivering nothing, with no counter moving."""

    async def scenario() -> tuple[list[str], list[str], bool]:
        stalled = _Socket(hangs=True)
        healthy = _Socket()
        rt._channels["space:s"] = {(stalled, "d-stalled"), (healthy, "d-healthy")}  # type: ignore[arg-type]
        try:
            original, rt.SEND_TIMEOUT = rt.SEND_TIMEOUT, 0.05
            try:
                await rt._broadcast_local("space:s", "payload")
            finally:
                rt.SEND_TIMEOUT = original
            return healthy.received, stalled.received, stalled.closed
        finally:
            rt._channels.pop("space:s", None)
            rt._ws_channels.pop(stalled, None)  # type: ignore[arg-type]
            rt._ws_channels.pop(healthy, None)  # type: ignore[arg-type]

    healthy_got, stalled_got, stalled_closed = asyncio.run(scenario())
    assert healthy_got == ["payload"], "a healthy socket must not wait on a stalled one"
    assert stalled_got == []
    assert stalled_closed, "a socket that stopped reading is closed, not retried"


def test_the_origin_device_is_still_skipped():
    """Concurrency must not lose the exclusion that stops a device receiving its
    own push back."""

    async def scenario() -> tuple[list[str], list[str]]:
        author = _Socket()
        other = _Socket()
        rt._channels["user:u"] = {(author, "d-author"), (other, "d-other")}  # type: ignore[arg-type]
        try:
            await rt._broadcast_local("user:u", "payload", exclude_device="d-author")
            return author.received, other.received
        finally:
            rt._channels.pop("user:u", None)

    author_got, other_got = asyncio.run(scenario())
    assert author_got == []
    assert other_got == ["payload"]


# ── A socket's channel set follows membership on its own ─────────────


def test_a_socket_moves_between_channels_in_one_step():
    """Rebinding must not pass through "on no channel at all".

    The set used to be changed as unregister-then-register. Between the two the
    socket was on nothing, and an event delivered in that window is not late,
    it is lost - pub/sub has no replay. Channels the socket keeps must also
    survive the move untouched, or a rebind would silently drop the user
    channel every membership change arrives on.
    """

    async def scenario():
        ws = _Socket()
        try:
            await rt._register(ws, "d1", ["user:u", "space:old"])  # type: ignore[arg-type]
            await rt._rebind(ws, ["user:u", "space:new"])  # type: ignore[arg-type]
            return (
                set(rt._ws_channels.get(ws, set())),  # type: ignore[arg-type]
                {ch for ch, members in rt._channels.items() if (ws, "d1") in members},
            )
        finally:
            await rt._unregister(ws)  # type: ignore[arg-type]

    bound, indexed = asyncio.run(scenario())
    assert bound == {"user:u", "space:new"}
    assert indexed == bound, "the forward index and the per-socket set disagree"


def test_a_socket_that_left_is_not_resurrected_by_a_rebind():
    """Resolving a channel set is a database round trip, and the socket can
    close during it. Re-adding one whose teardown has already run puts a dead
    entry in the hub that nothing ever removes."""

    async def scenario():
        ws = _Socket()
        await rt._register(ws, "d1", ["user:u"])  # type: ignore[arg-type]
        await rt._unregister(ws)  # type: ignore[arg-type]
        await rt._rebind(ws, ["user:u", "space:new"])  # type: ignore[arg-type]
        return ws in rt._ws_channels, "space:new" in rt._channels  # type: ignore[operator]

    still_bound, channel_recreated = asyncio.run(scenario())
    assert not still_bound
    assert not channel_recreated


def test_membership_re_resolves_every_local_socket_of_that_user():
    """The hardening. A space created or joined under an open socket is a
    channel that socket is not on, and everything published to it - entries,
    comments, withdrawals, presence, the next membership change - goes to an
    audience the user is missing from. The client is asked to send
    `resubscribe`, but the fan-out cannot depend on which build is at the far
    end, so the server re-resolves when the event goes past. Both of the user's
    devices move, not just the one that acted."""

    async def scenario():
        phone, laptop, stranger = _Socket(), _Socket(), _Socket()
        resolved = rt._resolve_channels

        async def fake_resolve(user_id: str) -> list[str]:
            return [f"user:{user_id}", rt.BROADCAST_CHANNEL, "space:fresh"]

        rt._resolve_channels = fake_resolve  # type: ignore[assignment]
        try:
            await rt._register(phone, "d1", ["user:u", rt.BROADCAST_CHANNEL])  # type: ignore[arg-type]
            await rt._register(laptop, "d2", ["user:u", rt.BROADCAST_CHANNEL])  # type: ignore[arg-type]
            await rt._register(stranger, "d3", ["user:other", rt.BROADCAST_CHANNEL])  # type: ignore[arg-type]
            await rt._resync_user_channels("u")
            return (
                set(rt._ws_channels[phone]),  # type: ignore[index]
                set(rt._ws_channels[laptop]),  # type: ignore[index]
                set(rt._ws_channels[stranger]),  # type: ignore[index]
            )
        finally:
            rt._resolve_channels = resolved  # type: ignore[assignment]
            for sock in (phone, laptop, stranger):
                await rt._unregister(sock)  # type: ignore[arg-type]

    phone_on, laptop_on, stranger_on = asyncio.run(scenario())
    assert "space:fresh" in phone_on
    assert "space:fresh" in laptop_on, "a user's other devices are on the same spaces"
    assert stranger_on == {"user:other", rt.BROADCAST_CHANNEL}, "somebody else must not move"


def test_a_replica_holding_no_socket_for_that_user_does_no_work():
    """Every replica sees every membership event, and only one of them holds
    the socket. The others must not each spend a query finding that out."""

    async def scenario() -> bool:
        touched = False
        resolved = rt._resolve_channels

        async def fake_resolve(user_id: str) -> list[str]:
            nonlocal touched
            touched = True
            return []

        rt._resolve_channels = fake_resolve  # type: ignore[assignment]
        try:
            rt._schedule_resync("nobody-here")
            await rt._resync_user_channels("nobody-here")
            return touched
        finally:
            rt._resolve_channels = resolved  # type: ignore[assignment]

    assert asyncio.run(scenario()) is False


def test_a_departing_socket_reports_the_channels_it_ended_up_on():
    """Teardown announces the user offline to their spaces. Since the server
    can move a socket mid-connection, the set resolved at connect is not the
    one to announce to - a space joined since would never hear it."""

    async def scenario() -> list[str]:
        ws = _Socket()
        await rt._register(ws, "d1", ["user:u", "space:old"])  # type: ignore[arg-type]
        await rt._rebind(ws, ["user:u", "space:old", "space:joined-since"])  # type: ignore[arg-type]
        return await rt._unregister(ws)  # type: ignore[arg-type]

    left = asyncio.run(scenario())
    assert rt._space_channels(left) == ["space:joined-since", "space:old"]


def test_the_request_pool_is_bounded():
    """An unbounded pool opened one connection per concurrent coroutine: a hard
    cap and an error on a managed instance, per-connection buffers on a
    self-hosted one."""
    assert client_kwargs(blocking_reads=False)["max_connections"] == 24
    assert client_kwargs(blocking_reads=True)["max_connections"] == 2
