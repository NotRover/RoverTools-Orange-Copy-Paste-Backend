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
from datetime import UTC, datetime
from typing import Awaitable, cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis, from_url
from sqlalchemy import select

from src.auth.tokens import decode_supabase_token
from src.database import AsyncSessionLocal
from src.spaces.models import SpaceMembership
from src.redis_client import get_redis_pool

logger = logging.getLogger(__name__)
router = APIRouter(tags=["realtime"])

PING_INTERVAL = 25  # seconds between server pings
# Every socket joins this, which is what makes a broadcast one publish instead
# of one per connected user.
BROADCAST_CHANNEL = "broadcast:all"
PRESENCE_TTL = 300  # seconds; refreshed on every client message/pong


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def presence_key(user_id: str, device_id: str) -> str:
    return f"presence:{user_id}:{device_id}"


def devices_set_key(user_id: str) -> str:
    return f"user:{user_id}:devices"


async def user_is_online(redis: Redis, user_id: str) -> bool:
    """True when at least one of the user's devices holds a live presence key.

    The device set can outlive a socket that died without a clean close, so
    membership alone is not proof — each candidate is checked against its
    TTL key, which only a connected (and heartbeating) device keeps alive.
    """
    device_ids = await cast(Awaitable[set[str]], redis.smembers(devices_set_key(user_id)))
    for device_id in device_ids:
        if await redis.exists(presence_key(user_id, device_id)):
            return True
    return False


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


async def _broadcast_local(channel: str, raw: str, exclude_device: str | None = None) -> None:
    async with _lock:
        targets = list(_channels.get(channel, set()))
    dead: list[tuple[WebSocket, str]] = []
    for ws, device_id in targets:
        if exclude_device and device_id == exclude_device:
            continue
        try:
            await ws.send_text(raw)
        except Exception:
            dead.append((ws, device_id))
    for item in dead:
        await _unregister(item[0])


# ── Redis pub/sub bridge ─────────────────────────────────────────────────────────


async def start_listener(redis_url: str) -> None:
    """Long-lived task (started in app lifespan): forward Redis messages to local sockets."""
    redis = from_url(redis_url, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.psubscribe("user:*", "space:*", "broadcast:*")
    try:
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
        pass
    finally:
        await pubsub.aclose()
        await redis.aclose()


# ── Publish helpers (called by sync / spaces / settings services) ────────────────


async def publish(redis: Redis, channel: str, event: str, payload: dict, origin_device: str | None = None) -> None:
    data: dict = {"event": event, "payload": payload}
    if origin_device:
        data["_origin_device"] = origin_device
    await redis.publish(channel, json.dumps(data))


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
    await cast(Awaitable[int], redis.sadd(devices_set_key(user_id), device_id))
    await redis.set(presence_key(user_id, device_id), "1", ex=PRESENCE_TTL)
    await publish(redis, f"user:{user_id}", "device:online", {"device_id": device_id})
    await publish_user_presence(redis, _space_channels(channels), user_id, True)

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=PING_INTERVAL)
                msg = json.loads(raw)
                if msg.get("event") in ("pong", "ack"):
                    await redis.expire(presence_key(user_id, device_id), PRESENCE_TTL)
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
        await cast(Awaitable[int], redis.srem(devices_set_key(user_id), device_id))
        await redis.delete(presence_key(user_id, device_id))
        await publish(redis, f"user:{user_id}", "device:offline", {"device_id": device_id})
        # Only the user's last device going away makes them offline to others.
        if not await user_is_online(redis, user_id):
            await publish_user_presence(redis, _space_channels(channels), user_id, False)
