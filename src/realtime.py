"""Realtime layer: WebSocket endpoint, in-process connection hub, Redis pub/sub
fan-out, and device presence.

Multi-replica model: each API process keeps its own in-memory hub of local
sockets and subscribes to Redis (`user:*`, `space:*`). Any process publishes an
event to Redis; every process forwards it to its own local sockets on that
channel. No sticky sessions needed.

Presence is connection-driven: connect → `device:online` immediately; clean
disconnect → `device:offline` immediately. A per-device Redis key with a TTL
(refreshed by heartbeat) lets the maintenance sweeper in `background.py` emit
`device:offline` for sockets that died without a clean close.
"""

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Awaitable, cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis, from_url
from redis.exceptions import RedisError
from sqlalchemy import select

from src.auth.tokens import verify_supabase_token
from src.database import AsyncSessionLocal
from src.dependencies import check_device
from src.spaces.models import SpaceMembership
from src.redis_client import client_kwargs, get_redis_pool

logger = logging.getLogger(__name__)
router = APIRouter(tags=["realtime"])

PING_INTERVAL = 25  # seconds between server pings
# Longest a single socket may hold up fan-out to everyone else on this replica.
# See `_broadcast_local` for why a bound has to exist at all.
SEND_TIMEOUT = 5
# Every socket joins this, which is what makes a broadcast one publish instead
# of one per connected user.
BROADCAST_CHANNEL = "broadcast:all"
PRESENCE_TTL = 300  # seconds; refreshed on every client message/pong
# Longest a channel re-resolution may spend in the database before it is
# abandoned. Same reasoning as SEND_TIMEOUT: the work is off the listener's
# path, but an unbounded query still holds a connection indefinitely.
RESYNC_TIMEOUT = 10

# The socket handshake: the client has this long after the upgrade to send its
# `{"type": "auth", ...}` message, and every failure closes with AUTH_FAILED and
# says nothing else, so a probe learns nothing about why.
AUTH_TIMEOUT = 10
AUTH_FAILED = 4401
# Sockets one user may hold on one replica. Generous for real use (one per
# running app); what it stops is one token opening sockets until the replica
# runs out of file descriptors.
MAX_SOCKETS_PER_USER = 8
TOO_MANY_SOCKETS = 4429
# Shortest gap between two honoured `resubscribe` messages on one socket. Each
# costs a database query, so an unthrottled client could make the socket a
# query pump.
RESUBSCRIBE_MIN_INTERVAL = 5

# How long the pub/sub listener waits before re-subscribing, and the ceiling it
# backs off to. A Redis that is restarting comes back in seconds; one that is
# gone for the afternoon should not be probed every second all afternoon.
LISTENER_RETRY_SECONDS = 1
LISTENER_RETRY_CEILING_SECONDS = 30

# Fan-out events Redis would not take. Exposed as a gauge by /internal/metrics:
# swallowing a publish keeps the request honest, but a silent delivery gap needs
# somewhere to show up other than a support ticket.
_dropped_events = 0


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def presence_key(user_id: str, device_id: str) -> str:
    return f"presence:{user_id}:{device_id}"


def devices_set_key(user_id: str) -> str:
    return f"user:{user_id}:devices"


async def _assert_presence(redis: Redis, user_id: str, device_id: str) -> None:
    """State that this device is here, from scratch, in one round trip.

    Used by both the connect path and every heartbeat, and it *rewrites* both
    keys rather than extending a TTL. That difference is the whole point.

    The heartbeat used to call `EXPIRE` on the presence key, and `EXPIRE` on a
    key that no longer exists returns 0 and does nothing. So any loss of the key
    was permanent for the life of the socket: a still-connected device kept
    checking in, every check did nothing, and the device read as offline on
    `GET /auth/devices` and to every member of its spaces until the app was
    restarted. The sweeper could not repair it either — it only ever removes.

    Keys are lost in ordinary operation, not just in disasters: the instance is
    configured with an LRU eviction policy, so presence keys are eviction
    candidates by design; a Redis restart drops the lot; and a link that misses
    pongs for longer than `PRESENCE_TTL` lets the key lapse before recovering.

    The device set is rewritten for the same reason — `SADD` is already
    idempotent, but a restart takes the set with the keys, and `user_is_online`
    and the sweeper both read the set rather than the keys.
    """
    pipe = redis.pipeline(transaction=False)
    pipe.sadd(devices_set_key(user_id), device_id)
    pipe.set(presence_key(user_id, device_id), "1", ex=PRESENCE_TTL)
    await pipe.execute()


async def user_is_online(redis: Redis, user_id: str, exclude_device: str | None = None) -> bool:
    """True when at least one of the user's devices holds a live presence key.

    The device set can outlive a socket that died without a clean close, so
    membership alone is not proof — each candidate is checked against its
    TTL key, which only a connected (and heartbeating) device keeps alive.

    `exclude_device` answers "is anyone *else* still here", which is the question
    a disconnecting socket actually has. It lets the teardown path ask before it
    removes itself, so the answer survives a Redis blip on the way out — see the
    comment in the endpoint's `finally`.
    """
    device_ids = await cast(Awaitable[set[str]], redis.smembers(devices_set_key(user_id)))
    for device_id in device_ids:
        if exclude_device is not None and device_id == exclude_device:
            continue
        if await redis.exists(presence_key(user_id, device_id)):
            return True
    return False


async def presence_for_users(redis: Redis, user_ids: Sequence[str]) -> set[str]:
    """Which of `user_ids` have at least one live device, in two round trips.

    For response *fields* only. A presence store that is unreachable reports
    everyone offline rather than failing the request: `online` is documented as a
    snapshot and no client authorizes anything on it, so a wrong hint beats a 500
    on a list of spaces that Postgres answered in full.

    Never use this where the answer drives a decision. Both callers of
    `user_is_online` do - one publishes user-offline on socket teardown, the other
    sweeps evicted devices - and reporting offline there tells a whole space that
    a user with live devices went away, with nothing to correct it, since the
    online announcement only fires on connect.

    Batched rather than a loop over `user_is_online`, because the per-user form is
    one SMEMBERS plus one EXISTS per device. Called once per member of every space
    in a list, that is a round trip count that grows with the account - and with a
    read timeout configured, a slow-but-alive Redis multiplies its own timeout by
    that number. Two pipelines cannot.
    """
    unique = list(dict.fromkeys(user_ids))
    if not unique:
        return set()
    try:
        devices = redis.pipeline(transaction=False)
        for user_id in unique:
            devices.smembers(devices_set_key(user_id))
        device_sets = await devices.execute()

        # The device set outlives a socket that died without a clean close, so
        # membership is not presence: each candidate is checked against the TTL
        # key only a heartbeating device keeps alive.
        live = redis.pipeline(transaction=False)
        owners: list[str] = []
        for user_id, device_ids in zip(unique, device_sets, strict=True):
            for device_id in device_ids or ():
                live.exists(presence_key(user_id, device_id))
                owners.append(user_id)
        if not owners:
            return set()
        results = await live.execute()
        return {owner for owner, is_live in zip(owners, results, strict=True) if is_live}
    except RedisError:
        logger.warning(
            "presence store unavailable; reporting %d user(s) offline", len(unique)
        )
        return set()


# ── In-process connection hub ───────────────────────────────────────────────────

# channel_name → set of (websocket, device_id)
_channels: dict[str, set[tuple[WebSocket, str]]] = defaultdict(set)
_ws_channels: dict[WebSocket, set[str]] = {}
_ws_device: dict[WebSocket, str] = {}
# user_id -> that user's sockets on this replica, counted against
# MAX_SOCKETS_PER_USER. Kept apart from `_channels` so a slot is held from the
# moment the handshake passes, before the channel set is bound.
_user_sockets: dict[str, set[WebSocket]] = defaultdict(set)
_lock = asyncio.Lock()


async def _reserve_slot(user_id: str, ws: WebSocket) -> bool:
    async with _lock:
        held = _user_sockets[user_id]
        if len(held) >= MAX_SOCKETS_PER_USER:
            if not held:
                del _user_sockets[user_id]
            return False
        held.add(ws)
        return True


async def _release_slot(user_id: str, ws: WebSocket) -> None:
    async with _lock:
        held = _user_sockets.get(user_id)
        if held is None:
            return
        held.discard(ws)
        if not held:
            del _user_sockets[user_id]


def _bind_locked(ws: WebSocket, device_id: str, channels: list[str]) -> None:
    """Put `ws` on exactly `channels`, dropping any it no longer belongs on.

    One step, and the caller holds `_lock` for all of it. Changing a channel
    set as unregister-then-register leaves a window in which the socket is on
    no channel at all, and anything delivered in that window is not delayed,
    it is gone - pub/sub does not replay.
    """
    wanted = set(channels)
    for ch in _ws_channels.get(ws, set()) - wanted:
        _channels[ch].discard((ws, device_id))
        if not _channels[ch]:
            del _channels[ch]
    _ws_device[ws] = device_id
    _ws_channels[ws] = wanted
    for ch in wanted:
        _channels[ch].add((ws, device_id))


async def _register(ws: WebSocket, device_id: str, channels: list[str]) -> None:
    async with _lock:
        _bind_locked(ws, device_id, channels)


async def _rebind(ws: WebSocket, channels: list[str]) -> None:
    """Move an already-registered socket onto `channels`.

    A no-op for a socket that has gone. Resolving a channel set means a
    database round trip, and the socket can close during it — re-adding it
    would put a dead entry in `_channels` that nothing ever removes, since its
    teardown has already run.
    """
    async with _lock:
        device_id = _ws_device.get(ws)
        if device_id is None:
            return
        _bind_locked(ws, device_id, channels)


async def _unregister(ws: WebSocket) -> list[str]:
    """Remove `ws` from the hub, and report what it was on.

    The channel set is returned because it is no longer safe to keep a copy:
    the server re-resolves a socket's channels when membership changes, so
    whatever the endpoint resolved at connect may not be where this socket
    ended up — and the departure has to be announced to the spaces it was
    actually in.
    """
    async with _lock:
        device_id = _ws_device.pop(ws, None)
        channels = _ws_channels.pop(ws, set())
        for ch in channels:
            _channels[ch].discard((ws, device_id))  # type: ignore[arg-type]
            if not _channels[ch]:
                del _channels[ch]
        return sorted(channels)


def _space_channels(channels: list[str]) -> list[str]:
    """Just the space channels — the user's own channel carries device-level
    presence already and must not get the per-user event too."""
    return [c for c in channels if c.startswith("space:")]


async def _resolve_channels(user_id: str) -> list[str]:
    """Where a socket for `user_id` belongs: their own channel, the broadcast
    channel, and one per space they are a member of."""
    channels = [f"user:{user_id}", BROADCAST_CHANNEL]
    async with AsyncSessionLocal() as db:
        memberships = await db.scalars(
            select(SpaceMembership).where(SpaceMembership.user_id == uuid.UUID(user_id))
        )
        channels.extend(f"space:{m.space_id}" for m in memberships.all())
    return channels


async def _resync_user_channels(user_id: str) -> None:
    """Put every local socket of `user_id` back on the channels their
    memberships now imply.

    The hole this closes: the channel set is resolved once, at connect. A space
    joined — or created — after that is a channel the socket is not on, and
    everything published to it goes to an audience the user is missing from:
    the entries other members share, their comments, withdrawals, presence, and
    the next membership change, which is the very event that was supposed to
    fix this. Nothing recovers until the socket reconnects.

    The client is expected to answer a membership event with `resubscribe`, and
    still does. This does not replace that so much as stop depending on it —
    correctness of the fan-out should not rest on which build happens to be at
    the far end of the socket.

    Only ever driven by an event on the user's *own* channel, which is exactly
    the case "your membership changed". A membership event on a space channel
    is news about somebody else, and re-resolving every member on it would
    spend a query per member to learn nothing.
    """
    async with _lock:
        sockets = list(_channels.get(f"user:{user_id}", set()))
    if not sockets:
        return
    try:
        channels = await asyncio.wait_for(_resolve_channels(user_id), timeout=RESYNC_TIMEOUT)
    except Exception:
        # Leaves the socket on its old channel set, which is where a client
        # that sends `resubscribe` recovers, and where a reconnect does anyway.
        logger.exception("could not re-resolve channels for user %s", user_id)
        return
    for ws, _ in sockets:
        await _rebind(ws, channels)


# Held so the event loop does not collect a resync mid-flight; each drops
# itself on completion.
_resync_tasks: set[asyncio.Task] = set()


def _schedule_resync(user_id: str) -> None:
    """Run a resync off the listener's critical path.

    Not awaited by the caller on purpose: the listener does not read the next
    Redis message until dispatch returns, so a database query taken in line
    would let a slow Postgres do to fan-out what a stalled socket used to —
    see `_broadcast_local`.
    """
    if not user_id or f"user:{user_id}" not in _channels:
        return  # not a user this replica holds a socket for
    task = asyncio.create_task(_resync_user_channels(user_id))
    _resync_tasks.add(task)
    task.add_done_callback(_resync_tasks.discard)


async def _close_device_sockets(user_id: str, device_id: str) -> None:
    """Close every local socket `device_id` holds. Its endpoint loop then runs the
    usual teardown (presence, `device:offline`)."""
    async with _lock:
        targets = [ws for ws, dev in _channels.get(f"user:{user_id}", set()) if dev == device_id]
    for ws in targets:
        try:
            await ws.close(code=AUTH_FAILED)
        except Exception:
            pass  # already gone


def _schedule_close_device(user_id: str, device_id: str) -> None:
    if f"user:{user_id}" not in _channels:
        return  # not a user this replica holds a socket for
    task = asyncio.create_task(_close_device_sockets(user_id, device_id))
    _resync_tasks.add(task)
    task.add_done_callback(_resync_tasks.discard)


async def _send_to_one(ws: WebSocket, raw: str) -> bool:
    """Deliver to a single socket. False means give up on it.

    The timeout is the load-bearing part: `send_text` applies the transport's
    backpressure, so a client that has stopped reading blocks here for as long
    as it likes unless something bounds it.
    """
    try:
        await asyncio.wait_for(ws.send_text(raw), timeout=SEND_TIMEOUT)
        return True
    except Exception:
        return False


def _trim_entry_for(ws: WebSocket, data: dict) -> str:
    """A `sync:entry` event as one socket may see it - the socket-side twin of
    `sync.service.entry_view`.

    The author's sockets hold `user:{author}` and get the row whole. Anyone
    else reached it through a space, and their socket's channel set is exactly
    the spaces they are in: `space_ids` shrinks to those, `wrapped_keys` to
    those spaces' wraps, and the personal wrap goes. Trimmed per socket rather
    than per channel because a member of two of the entry's spaces is on both
    channels and must see the same view from each.
    """
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return json.dumps(data)
    mine = _ws_channels.get(ws, set())
    author = payload.get("user_id")
    if author and f"user:{author}" in mine:
        return json.dumps(data)
    shown = [s for s in (payload.get("space_ids") or []) if f"space:{s}" in mine]
    try:
        keys: Any = json.loads(payload.get("wrapped_keys") or "{}")
    except (json.JSONDecodeError, TypeError):
        keys = {}
    kept = {str(s): keys[str(s)] for s in shown if str(s) in keys} if isinstance(keys, dict) else {}
    trimmed = {**payload, "space_ids": shown, "wrapped_keys": json.dumps(kept)}
    return json.dumps({**data, "payload": trimmed})


async def _broadcast_local(
    channel: str,
    raw: str,
    exclude_device: str | None = None,
    trim: dict | None = None,
) -> None:
    """Deliver one event to this replica's sockets on `channel`.

    `trim` is the decoded event when it is a `sync:entry`: each socket then gets
    its own view of it (see `_trim_entry_for`) instead of `raw`.

    Concurrently, and with a per-socket timeout, because this is awaited by the
    pub/sub listener and used to be neither. Sending in sequence meant one
    backpressured client stalled delivery to every other socket on the replica —
    and because the listener does not read the next Redis message until this
    returns, the stall propagated backwards into Redis: the pub/sub client output
    buffer filled, Redis dropped the subscriber (its default limits are 32 MB
    hard, 8 MB sustained for 60 s), and the replica went on serving HTTP normally
    while delivering nothing at all to any socket it held. The supervised
    reconnect in `start_listener` recovers the connection but not the events —
    pub/sub has no replay, so everything published during the gap was gone. None
    of it showed up in `orange_realtime_events_dropped_total`, which only counts
    publish-side failures.

    `SEND_TIMEOUT` therefore bounds how long the bus can be held up by its
    slowest reader. The bound is what matters, not its exact value: filling an
    8 MB buffer inside it would take on the order of a thousand events a second
    at the sizes actually seen in this system.

    A socket that times out is unregistered and closed rather than retried. It
    has stopped reading, so the next event would stall on it too; closing ends
    the endpoint's receive loop, which runs the presence teardown and lets the
    client reconnect and pull.
    """
    async with _lock:
        targets = [
            (ws, device_id)
            for ws, device_id in _channels.get(channel, set())
            if not (exclude_device and device_id == exclude_device)
        ]
    if not targets:
        return

    delivered = await asyncio.gather(
        *(_send_to_one(ws, _trim_entry_for(ws, trim) if trim else raw) for ws, _ in targets)
    )
    for (ws, _), ok in zip(targets, delivered, strict=True):
        if ok:
            continue
        await _unregister(ws)
        try:
            await ws.close(code=1011)
        except Exception:
            pass  # already gone, or never going to answer — either way, done with it


# ── Redis pub/sub bridge ─────────────────────────────────────────────────────────


async def start_listener(redis_url: str) -> None:
    """Long-lived task (started in app lifespan): forward Redis messages to local sockets.

    Supervised, because this is the one Redis failure with no HTTP symptom. One
    `ConnectionError` out of `listen()` used to end the task, and the replica then
    served every request normally while delivering nothing to any socket it held:
    no live updates, no error, nothing to alert on. It reconnects instead, and
    keeps its own subscription rather than borrowing the request pool, since a
    blocking read has to be configured differently from a request.
    """
    delay = LISTENER_RETRY_SECONDS
    while True:
        redis = from_url(redis_url, **client_kwargs(blocking_reads=True))
        pubsub = redis.pubsub()
        try:
            # Inside the try: a failure to subscribe is exactly the failure this
            # loop exists for, and it used to escape with nothing closed.
            await pubsub.psubscribe("user:*", "space:*", "broadcast:*")
            delay = LISTENER_RETRY_SECONDS
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue
                channel = message.get("channel", "")
                data = message.get("data", "")
                if not isinstance(data, str):
                    continue
                try:
                    parsed = json.loads(data)
                    exclude_device: str | None = parsed.pop("_origin_device", None)
                    trim = parsed if parsed.get("event") == "sync:entry" else None
                    await _broadcast_local(channel, json.dumps(parsed), exclude_device, trim)
                    # A membership event on a user's own channel is that user's
                    # space list changing under an open socket. Acting on it
                    # here is what makes the channel set self-healing on every
                    # replica, whatever the client does with the same event.
                    if (
                        parsed.get("event") == "space:membership_changed"
                        and channel.startswith("user:")
                    ):
                        _schedule_resync(channel.removeprefix("user:"))
                    # Sent to the device first (above), so it learns why, then
                    # its sockets on this replica are closed.
                    if parsed.get("event") == "device:revoked" and channel.startswith("user:"):
                        revoked = (parsed.get("payload") or {}).get("device_id")
                        if isinstance(revoked, str):
                            _schedule_close_device(channel.removeprefix("user:"), revoked)
                except Exception:
                    logger.exception("pubsub dispatch error on channel %s", channel)
        except asyncio.CancelledError:
            # Shutdown, not a fault. Re-raised so the lifespan's cancel actually
            # cancels: swallowing it reported the task as having finished
            # normally and hid a listener that stopped on its own.
            raise
        except RedisError:
            logger.exception("pubsub listener lost Redis; reconnecting in %ss", delay)
        finally:
            await pubsub.aclose()
            await redis.aclose()
        await asyncio.sleep(delay)
        delay = min(delay * 2, LISTENER_RETRY_CEILING_SECONDS)


# ── Publish helpers (called by sync / spaces / settings services) ────────────────


# Events whose loss a client cannot recover from by pulling. Logged louder
# because that is the whole difference: everything else arrives late, these two
# do not arrive.
_UNRECOVERABLE_EVENTS = frozenset({"space:rekey", "space:entry_removed"})


async def publish(redis: Redis, channel: str, event: str, payload: dict, origin_device: str | None = None) -> None:
    """Fan an event out to every replica. Best-effort by design.

    Every `publish_*` helper below funnels through here, and every one of them is
    called *after* the write it announces has been committed. Letting a Redis
    blip raise would answer 500 for work that succeeded, and the client would
    retry a push that is already stored.

    What that costs, stated plainly: most events are only delayed, because the
    next pull carries the same change. `space:entry_removed` is not - a pull
    matches rows that still carry the space id, so once the id is gone the
    withdrawal is invisible and a member keeps a copy of something taken down.
    A retry from the client emits nothing either, because the id is already off
    the row. Closing that needs a durable outbox, which is a bigger decision
    than this function.
    """
    global _dropped_events
    data: dict = {"event": event, "payload": payload}
    if origin_device:
        data["_origin_device"] = origin_device
    try:
        await redis.publish(channel, json.dumps(data))
    except RedisError as exc:
        _dropped_events += 1
        level = logging.ERROR if event in _UNRECOVERABLE_EVENTS else logging.WARNING
        logger.log(level, "dropped %s on %s: %s", event, channel, exc)


def dropped_events() -> int:
    """Fan-out events lost to an unreachable Redis since this process started."""
    return _dropped_events


async def publish_sync_entry(
    redis: Redis, user_id: str, device_id: str, entry_payload: dict, space_ids: list[str]
) -> None:
    await publish(redis, f"user:{user_id}", "sync:entry", entry_payload, origin_device=device_id)
    for sid in space_ids:
        await publish(redis, f"space:{sid}", "sync:entry", entry_payload, origin_device=device_id)



async def publish_space_entry_removed(
    redis: Redis,
    space_id: str,
    client_id: str,
    entry_type: str,
    author_id: str,
    removed_by: str,
    origin_device: str | None = None,
) -> None:
    """Tell a space that one of its entries is no longer in it — taken down by
    the space owner, or un-shared by whoever posted it.

    Pull only matches rows that still carry the space id, so once the id is
    gone the row is invisible: a member already holding a copy would never
    learn it was withdrawn.

    `removed_by` is what separates the two cases. Without it a member can only
    guess, and the client guessed "the owner took it down" every time — so an
    author withdrawing their own post was reported to everyone else as
    moderation.
    """
    payload = {
        "space_id": space_id,
        "client_id": client_id,
        "entry_type": entry_type,
        "author_id": author_id,
        "removed_by": removed_by,
    }
    await publish(redis, f"space:{space_id}", "space:entry_removed", payload, origin_device=origin_device)


async def publish_space_comment(
    redis: Redis,
    space_id: str,
    action: str,
    payload: dict,
    origin_device: str | None = None,
) -> None:
    """Fan a comment out to the space it was written in.

    Carries the ciphertext rather than a "go and fetch it" nudge: every member
    on the channel can already unwrap it, and a thread that is open on screen
    should fill in without a round trip. `action` is `created` or `deleted`.
    """
    await publish(redis, f"space:{space_id}", "space:comment", {"action": action, **payload}, origin_device)


async def publish_space_membership_changed(redis: Redis, space_id: str, action: str, affected_user_id: str) -> None:
    payload = {"space_id": space_id, "action": action, "user_id": affected_user_id}
    await publish(redis, f"space:{space_id}", "space:membership_changed", payload)


async def publish_membership_changed_to_user(redis: Redis, user_id: str, space_id: str, action: str) -> None:
    """Same event, addressed to one user's own channel — for the joiner or the
    removed member, who isn't (or is no longer) subscribed to the space channel."""
    payload = {"space_id": space_id, "action": action, "user_id": user_id}
    await publish(redis, f"user:{user_id}", "space:membership_changed", payload)


async def publish_invite_received(redis: Redis, invitee_user_id: str, invite_payload: dict) -> None:
    await publish(redis, f"user:{invitee_user_id}", "invite:received", invite_payload)


async def publish_invite_updated(redis: Redis, user_id: str, invite_id: str, status: str, space_id: str) -> None:
    payload = {"invite_id": invite_id, "status": status, "space_id": space_id}
    await publish(redis, f"user:{user_id}", "invite:updated", payload)


async def publish_join_requested(redis: Redis, channel: str, request_payload: dict) -> None:
    """Somebody asked to join. Addressed to whoever may approve it.

    The channel is the caller's choice because the audience depends on the
    space's policy: the whole space channel when members may approve, the
    owner's own channel when they may not. Sending it to the space channel
    regardless would tell every member about a decision that is not theirs.
    """
    await publish(redis, channel, "space:join_requested", request_payload)


async def publish_join_decided(redis: Redis, user_id: str, space_id: str, decision: str) -> None:
    """The answer, addressed to the requester.

    They are not subscribed to the space channel - they are not in the space
    yet - so this goes to their own channel, the way an invite update does.
    """
    payload = {"space_id": space_id, "status": decision}
    await publish(redis, f"user:{user_id}", "space:join_decided", payload)


async def publish_space_rekey(redis: Redis, space_id: str, user_id: str, wrapped_space_keys: str) -> None:
    payload = {"space_id": space_id, "wrapped_space_keys": wrapped_space_keys}
    await publish(redis, f"user:{user_id}", "space:rekey", payload)


async def publish_space_history_opened(redis: Redis, space_id: str) -> None:
    """The owner opened this space's back catalogue to everyone.

    Members need this because a pull only asks for entries newer than its
    cursor: the rows that just became visible are older than that, so nothing
    would fetch them. The event is what tells a client to go back for them.
    """
    await publish(redis, f"space:{space_id}", "space:history_opened", {"space_id": space_id})



async def publish_user_presence(redis: Redis, space_channels: list[str], user_id: str, online: bool) -> None:
    """Tell a user's spaces that they came online or went fully offline.

    `device:online` is addressed to the user's own channel, so other members of
    a space never learn about it — their member list
    stays stuck at whatever the last REST snapshot said. This is the same fact,
    per-user rather than per-device, addressed to the people who can see it.
    """
    for channel in space_channels:
        await publish(redis, channel, "user:presence", {"user_id": user_id, "online": online})


async def publish_announcement(redis: Redis, user_id: str | None, announcement: dict) -> None:
    """Push a server-authored announcement, to one user or to everyone.

    Best-effort by design: this only reaches sockets that are connected right
    now, and the durable copy is the database row the client pulls on its next
    refresh.
    """
    channel = f"user:{user_id}" if user_id else BROADCAST_CHANNEL
    await publish(redis, channel, "announcement:new", announcement)


async def publish_settings_updated(redis: Redis, user_id: str, updated_at: int) -> None:
    await publish(redis, f"user:{user_id}", "settings:updated", {"updated_at": updated_at})


async def publish_device_revoked(redis: Redis, user_id: str, device_id: str) -> None:
    """Tell the user's devices that `device_id` was revoked. The revoked device
    signs itself out on it, and every replica closes that device's sockets."""
    await publish(redis, f"user:{user_id}", "device:revoked", {"device_id": device_id})


# ── WebSocket endpoint ───────────────────────────────────────────────────────────


async def _authenticate(websocket: WebSocket, redis: Redis) -> tuple[str, str, int] | None:
    """Read and check the handshake message. (user_id, device_id, exp), or None.

    Never logs the message: it carries the access token.
    """
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT)
        msg = json.loads(raw)
    except Exception:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "auth":
        return None
    token = msg.get("token")
    raw_device = msg.get("device_id")
    if not isinstance(token, str) or not isinstance(raw_device, str):
        return None
    try:
        payload = await verify_supabase_token(token)
        user_id = payload.get("sub")
        if not isinstance(user_id, str) or not user_id:
            return None
        device_id = uuid.UUID(raw_device)
        async with AsyncSessionLocal() as db:
            await check_device(db, redis, user_id, device_id)
        return user_id, str(device_id), int(payload["exp"])
    except Exception:
        return None


async def _close_at_expiry(websocket: WebSocket, exp: int) -> None:
    """Close the socket when the token it authenticated with expires. The client
    reconnects with a fresh token; a socket must not outlive its credential."""
    await asyncio.sleep(max(0.0, exp - time.time()))
    try:
        await websocket.close(code=AUTH_FAILED)
    except Exception:
        pass


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Realtime WebSocket: subscribes the device to its user and space channels.

    Handshake: the server accepts the upgrade, then waits up to `AUTH_TIMEOUT`
    seconds for `{"type": "auth", "token": "<jwt>", "device_id": "<uuid>"}`. The
    token must verify and the device must be the user's own, unrevoked device;
    the server then answers `{"type": "auth_ok"}`. Any failure closes with 4401
    and nothing else. The token is never accepted in the URL, where proxies and
    access logs would keep it.

    Emits `device:online` on connect and `device:offline` on disconnect, and
    relays fan-out events (sync, space, invite, settings) to the socket. Closed
    with 4401 when the token expires or the device is revoked, and with 4429 when
    the user already holds `MAX_SOCKETS_PER_USER` sockets on this replica.
    """
    await websocket.accept()
    redis: Redis = await get_redis_pool()  # type: ignore[assignment]

    auth = await _authenticate(websocket, redis)
    if auth is None:
        try:
            await websocket.close(code=AUTH_FAILED)
        except Exception:
            pass
        return
    user_id, device_id, exp = auth

    if not await _reserve_slot(user_id, websocket):
        try:
            await websocket.close(code=TOO_MANY_SOCKETS)
        except Exception:
            pass
        return

    expiry = asyncio.create_task(_close_at_expiry(websocket, exp))
    try:
        await _serve(websocket, redis, user_id, device_id)
    finally:
        expiry.cancel()
        await _release_slot(user_id, websocket)


async def _serve(websocket: WebSocket, redis: Redis, user_id: str, device_id: str) -> None:
    """The authenticated life of one socket."""
    try:
        await websocket.send_json({"type": "auth_ok"})
        channels = await _resolve_channels(user_id)
    except Exception:
        return
    await _register(websocket, device_id, channels)

    # Presence: mark online + set the TTL key the sweeper watches.
    #
    # Announced on every connect, not only on the offline→online edge. A socket
    # that dies without a close leaves its TTL key behind for up to PRESENCE_TTL,
    # so a client that reconnects inside that window looks like it was never
    # away and an edge-triggered publish says nothing — leaving every member's
    # list showing them offline until something else refreshes it. Repeats are
    # free: the client drops a presence update that changes nothing.
    await _assert_presence(redis, user_id, device_id)
    await publish(redis, f"user:{user_id}", "device:online", {"device_id": device_id})
    await publish_user_presence(redis, _space_channels(channels), user_id, True)

    last_resubscribe = 0.0
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=PING_INTERVAL)
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                # Anything but an object is ignored: `.get` on a list or a number
                # used to raise out of the loop and drop the socket.
                if not isinstance(msg, dict):
                    continue
                if msg.get("event") in ("pong", "ack"):
                    try:
                        await _assert_presence(redis, user_id, device_id)
                    except RedisError as exc:
                        # `except (WebSocketDisconnect, RuntimeError)` below does
                        # not catch this, so one blip used to break the loop and
                        # drop a perfectly live socket. Swallowing is right here:
                        # a missed refresh is reconciled by the sweeper, and the
                        # next pong refreshes the key anyway.
                        logger.warning("presence refresh failed for %s: %s", device_id, exc)
                elif msg.get("event") == "resubscribe":
                    # Membership changed mid-connection (join/leave/invite
                    # accept). The server re-resolves on its own when the
                    # membership event goes past, so this is no longer what
                    # makes space fan-out work — it is the client saying it
                    # believes it is stale, which stays supported because it
                    # costs one query and is the client's only way to say so.
                    # Throttled per socket, since that query is the cost.
                    now = time.monotonic()
                    if now - last_resubscribe < RESUBSCRIBE_MIN_INTERVAL:
                        continue
                    last_resubscribe = now
                    await _rebind(websocket, await _resolve_channels(user_id))
            except asyncio.TimeoutError:
                await websocket.send_json({"event": "ping", "payload": {"server_ts": _now_ms()}})
            except KeyError:
                # A binary frame, which `receive_text` cannot read. Ignored like
                # any other message this protocol does not define.
                continue
            except (WebSocketDisconnect, RuntimeError):
                break
    finally:
        # The set as it actually ended up, not the one resolved at connect:
        # membership can move a socket between channels mid-connection, and a
        # stale local copy would announce the departure to the wrong spaces
        # while saying nothing to the ones it had joined since.
        channels = await _unregister(websocket)
        try:
            # Asked *before* the removal and excluding this device, so the answer
            # is in hand before anything is mutated. Asking afterwards — which is
            # what this used to do — meant a Redis blip between the SREM and the
            # check left the user advertised as online to their spaces with
            # nothing able to correct it: the sweeper re-derives presence from the
            # device set, and this device is no longer in it.
            others_online = await user_is_online(redis, user_id, exclude_device=device_id)
            await cast(Awaitable[int], redis.srem(devices_set_key(user_id), device_id))
            await redis.delete(presence_key(user_id, device_id))
            await publish(redis, f"user:{user_id}", "device:offline", {"device_id": device_id})
            # Only the user's last device going away makes them offline to others.
            if not others_online:
                await publish_user_presence(redis, _space_channels(channels), user_id, False)
        except RedisError as exc:
            # Nothing on a disconnect is worth raising out of a `finally`. These
            # calls were unguarded while the pong path above was not, so a blip
            # here escaped the endpoint. Whatever did not happen is reconciled by
            # the sweeper: the presence key expires on its own, and the sweep
            # publishes both events from the device set this device is still in.
            logger.warning("presence teardown failed for %s: %s", device_id, exc)
