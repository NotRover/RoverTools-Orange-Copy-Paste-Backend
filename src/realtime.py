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
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Awaitable, cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis, from_url
from redis.exceptions import RedisError
from sqlalchemy import select

from src.auth.tokens import decode_supabase_token
from src.database import AsyncSessionLocal
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
_lock = asyncio.Lock()


async def _register(ws: WebSocket, device_id: str, channels: list[str]) -> None:
    async with _lock:
        _ws_device[ws] = device_id
        _ws_channels[ws] = set(channels)
        for ch in channels:
            _channels[ch].add((ws, device_id))


async def _unregister(ws: WebSocket) -> None:
    async with _lock:
        device_id = _ws_device.pop(ws, None)
        for ch in _ws_channels.pop(ws, set()):
            _channels[ch].discard((ws, device_id))  # type: ignore[arg-type]
            if not _channels[ch]:
                del _channels[ch]


def _space_channels(channels: list[str]) -> list[str]:
    """Just the space channels — the user's own channel carries device-level
    presence already and must not get the per-user event too."""
    return [c for c in channels if c.startswith("space:")]


def connection_count() -> int:
    """Local (per-process) connected socket count."""
    return len(_ws_device)


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


async def _broadcast_local(channel: str, raw: str, exclude_device: str | None = None) -> None:
    """Deliver one event to this replica's sockets on `channel`.

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

    delivered = await asyncio.gather(*(_send_to_one(ws, raw) for ws, _ in targets))
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
                    await _broadcast_local(channel, json.dumps(parsed), exclude_device)
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


# ── WebSocket endpoint ───────────────────────────────────────────────────────────


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = "", device_id: str = ""):
    """Realtime WebSocket: subscribes the device to its user and space channels.

    Authenticates via `?token=` (Supabase JWT) and `?device_id=` query params.
    Emits `device:online` on connect and `device:offline` on disconnect, and
    relays fan-out events (sync, space, invite, settings) to the socket.
    """
    try:
        payload = decode_supabase_token(token)
    except Exception:
        await websocket.close(code=4001)
        return

    user_id: str = payload.get("sub", "")
    if not user_id or not device_id:
        await websocket.close(code=4001)
        return

    redis: Redis = await get_redis_pool()  # type: ignore[assignment]
    await websocket.accept()

    async def _resolve_channels() -> list[str]:
        # Channel list: the user's own channel, the broadcast channel, and
        # every space they belong to.
        channels = [f"user:{user_id}", BROADCAST_CHANNEL]
        async with AsyncSessionLocal() as db:
            memberships = await db.scalars(
                select(SpaceMembership).where(SpaceMembership.user_id == uuid.UUID(user_id))
            )
            channels.extend(f"space:{m.space_id}" for m in memberships.all())
        return channels

    channels = await _resolve_channels()
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

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=PING_INTERVAL)
                msg = json.loads(raw)
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
                    # Membership changed mid-connection (join/leave/invite accept):
                    # re-resolve the channel set so space fan-out starts (or stops)
                    # immediately instead of on the next reconnect.
                    await _unregister(websocket)
                    channels = await _resolve_channels()
                    await _register(websocket, device_id, channels)
            except asyncio.TimeoutError:
                await websocket.send_json({"event": "ping", "payload": {"server_ts": _now_ms()}})
            except (WebSocketDisconnect, RuntimeError):
                break
    finally:
        await _unregister(websocket)
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
