"""Realtime layer: WebSocket endpoint, in-process connection hub, Redis pub/sub
fan-out, and device presence.

Multi-replica model: each API process keeps its own in-memory hub of local
sockets and subscribes to Redis (`user:*`, `group:*`). Any process publishes an
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
from src.groups.models import GroupMembership
from src.redis_client import get_redis_pool

logger = logging.getLogger(__name__)
router = APIRouter(tags=["realtime"])

PING_INTERVAL = 25  # seconds between server pings
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


def _group_channels(channels: list[str]) -> list[str]:
    """Just the group channels — the user's own channel carries device-level
    presence already and must not get the per-user event too."""
    return [c for c in channels if c.startswith("group:")]


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
    await pubsub.psubscribe("user:*", "group:*")
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


# ── Publish helpers (called by sync / groups / settings services) ────────────────


async def publish(redis: Redis, channel: str, event: str, payload: dict, origin_device: str | None = None) -> None:
    data: dict = {"event": event, "payload": payload}
    if origin_device:
        data["_origin_device"] = origin_device
    await redis.publish(channel, json.dumps(data))


async def publish_sync_entry(
    redis: Redis, user_id: str, device_id: str, entry_payload: dict, group_ids: list[str]
) -> None:
    await publish(redis, f"user:{user_id}", "sync:entry", entry_payload, origin_device=device_id)
    for gid in group_ids:
        await publish(redis, f"group:{gid}", "sync:entry", entry_payload, origin_device=device_id)


async def publish_sync_delete(redis: Redis, user_id: str, device_id: str, server_id: str, deleted_at: int) -> None:
    payload = {"server_id": server_id, "deleted_at": deleted_at}
    await publish(redis, f"user:{user_id}", "sync:delete", payload, origin_device=device_id)


async def publish_group_membership_changed(redis: Redis, group_id: str, action: str, affected_user_id: str) -> None:
    payload = {"group_id": group_id, "action": action, "user_id": affected_user_id}
    await publish(redis, f"group:{group_id}", "group:membership_changed", payload)


async def publish_membership_changed_to_user(redis: Redis, user_id: str, group_id: str, action: str) -> None:
    """Same event, addressed to one user's own channel — for the joiner or the
    removed member, who isn't (or is no longer) subscribed to the group channel."""
    payload = {"group_id": group_id, "action": action, "user_id": user_id}
    await publish(redis, f"user:{user_id}", "group:membership_changed", payload)


async def publish_invite_received(redis: Redis, invitee_user_id: str, invite_payload: dict) -> None:
    await publish(redis, f"user:{invitee_user_id}", "invite:received", invite_payload)


async def publish_invite_updated(redis: Redis, user_id: str, invite_id: str, status: str, group_id: str) -> None:
    payload = {"invite_id": invite_id, "status": status, "group_id": group_id}
    await publish(redis, f"user:{user_id}", "invite:updated", payload)


async def publish_group_rekey(redis: Redis, group_id: str, user_id: str, wrapped_key: str) -> None:
    payload = {"group_id": group_id, "wrapped_group_key": wrapped_key}
    await publish(redis, f"user:{user_id}", "group:rekey", payload)


async def publish_sharing_invite(
    redis: Redis, invitee_user_id: str, share_group_id: str, from_user: dict, invite_code: str, expires_at: int
) -> None:
    payload = {
        "share_group_id": share_group_id,
        "from_user": from_user,
        "invite_code": invite_code,
        "expires_at": expires_at,
    }
    await publish(redis, f"user:{invitee_user_id}", "sharing:invite", payload)


async def publish_sharing_accepted(
    redis: Redis, owner_user_id: str, share_group_id: str, new_member: dict, wrapped_group_key: str | None
) -> None:
    payload = {"share_group_id": share_group_id, "new_member": new_member, "wrapped_group_key": wrapped_group_key}
    await publish(redis, f"user:{owner_user_id}", "sharing:accepted", payload)


async def publish_sharing_ended(redis: Redis, group_id: str, ended_by: str) -> None:
    await publish(redis, f"group:{group_id}", "sharing:ended", {"share_group_id": group_id, "ended_by": ended_by})


async def publish_sharing_scope_changed(redis: Redis, group_id: str, user_id: str, share_scope: str) -> None:
    payload = {"share_group_id": group_id, "user_id": user_id, "share_scope": share_scope}
    await publish(redis, f"group:{group_id}", "sharing:scope_changed", payload)


async def publish_user_presence(redis: Redis, group_channels: list[str], user_id: str, online: bool) -> None:
    """Tell a user's shared groups that they came online or went fully offline.

    `device:online` is addressed to the user's own channel, so other members of
    a pool group or Live Share session never learn about it — their member list
    stays stuck at whatever the last REST snapshot said. This is the same fact,
    per-user rather than per-device, addressed to the people who can see it.
    """
    for channel in group_channels:
        await publish(redis, channel, "user:presence", {"user_id": user_id, "online": online})


async def publish_settings_updated(redis: Redis, user_id: str, updated_at: int) -> None:
    await publish(redis, f"user:{user_id}", "settings:updated", {"updated_at": updated_at})


# ── WebSocket endpoint ───────────────────────────────────────────────────────────


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = "", device_id: str = ""):
    """Realtime WebSocket: subscribes the device to its user and group channels.

    Authenticates via `?token=` (Supabase JWT) and `?device_id=` query params.
    Emits `device:online` on connect and `device:offline` on disconnect, and
    relays fan-out events (sync, group, sharing, settings) to the socket.
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
        # Channel list: the user's own channel + every group they belong to.
        channels = [f"user:{user_id}"]
        async with AsyncSessionLocal() as db:
            memberships = await db.scalars(
                select(GroupMembership).where(GroupMembership.user_id == uuid.UUID(user_id))
            )
            channels.extend(f"group:{m.group_id}" for m in memberships.all())
        return channels

    channels = await _resolve_channels()
    await _register(websocket, device_id, channels)

    # Presence: mark online + set the TTL key the sweeper watches. Resolved
    # before the write, so "was the user already reachable" isn't answered by
    # this very connection.
    was_online = await user_is_online(redis, user_id)
    await cast(Awaitable[int], redis.sadd(devices_set_key(user_id), device_id))
    await redis.set(presence_key(user_id, device_id), "1", ex=PRESENCE_TTL)
    await publish(redis, f"user:{user_id}", "device:online", {"device_id": device_id})
    if not was_online:
        await publish_user_presence(redis, _group_channels(channels), user_id, True)

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=PING_INTERVAL)
                msg = json.loads(raw)
                if msg.get("event") in ("pong", "ack"):
                    await redis.expire(presence_key(user_id, device_id), PRESENCE_TTL)
                elif msg.get("event") == "resubscribe":
                    # Membership changed mid-connection (join/leave/invite accept):
                    # re-resolve the channel set so group fan-out starts (or stops)
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
            await publish_user_presence(redis, _group_channels(channels), user_id, False)
