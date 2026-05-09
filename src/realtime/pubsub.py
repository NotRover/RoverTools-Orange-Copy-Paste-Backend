"""
Redis pub/sub bridge.

start_listener() runs as a long-lived asyncio task (started in app lifespan).
It uses psubscribe to catch all user:* and group:* channels, then dispatches
incoming messages to the in-process hub.

publish_* helpers are called by sync/groups/sharing services after writes.
"""

import asyncio
import json
import logging

from redis.asyncio import Redis, from_url

from src.realtime import hub

logger = logging.getLogger(__name__)


async def start_listener(redis_url: str) -> None:
    """Long-running task: forward Redis pub/sub messages to WebSocket hub."""
    redis = from_url(redis_url, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.psubscribe("user:*", "group:*")
    try:
        async for message in pubsub.listen():
            if message.get("type") != "pmessage":
                continue
            channel: str = message.get("channel", "")
            data = message.get("data", "")
            if not isinstance(data, str):
                continue
            try:
                parsed = json.loads(data)
                exclude_device: str | None = parsed.pop("_origin_device", None)
                clean = json.dumps(parsed)
                await hub.broadcast(channel, clean, exclude_device)
            except Exception:
                logger.exception("pubsub dispatch error on channel %s", channel)
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.aclose()
        await redis.aclose()


# ── Publish helpers ───────────────────────────────────────────────────────────


async def publish(redis: Redis, channel: str, event: str, payload: dict, origin_device: str | None = None) -> None:
    data: dict = {"event": event, "payload": payload}
    if origin_device:
        data["_origin_device"] = origin_device
    await redis.publish(channel, json.dumps(data))


async def publish_sync_entry(redis: Redis, user_id: str, device_id: str, entry_payload: dict, group_ids: list[str]) -> None:
    await publish(redis, f"user:{user_id}", "sync:entry", entry_payload, origin_device=device_id)
    for gid in group_ids:
        await publish(redis, f"group:{gid}", "sync:entry", entry_payload, origin_device=device_id)


async def publish_sync_delete(redis: Redis, user_id: str, device_id: str, server_id: str, deleted_at: int) -> None:
    payload = {"server_id": server_id, "deleted_at": deleted_at}
    await publish(redis, f"user:{user_id}", "sync:delete", payload, origin_device=device_id)


async def publish_group_membership_changed(redis: Redis, group_id: str, action: str, affected_user_id: str) -> None:
    payload = {"group_id": group_id, "action": action, "user_id": affected_user_id}
    await publish(redis, f"group:{group_id}", "group:membership_changed", payload)


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
    redis: Redis,
    owner_user_id: str,
    share_group_id: str,
    new_member: dict,
    wrapped_group_key: str | None,
) -> None:
    payload = {
        "share_group_id": share_group_id,
        "new_member": new_member,
        "wrapped_group_key": wrapped_group_key,
    }
    await publish(redis, f"user:{owner_user_id}", "sharing:accepted", payload)


async def publish_sharing_ended(redis: Redis, group_id: str, ended_by: str) -> None:
    await publish(redis, f"group:{group_id}", "sharing:ended", {"share_group_id": group_id, "ended_by": ended_by})


async def publish_sharing_scope_changed(redis: Redis, group_id: str, user_id: str, share_scope: str) -> None:
    payload = {"share_group_id": group_id, "user_id": user_id, "share_scope": share_scope}
    await publish(redis, f"group:{group_id}", "sharing:scope_changed", payload)


async def publish_settings_updated(redis: Redis, user_id: str, updated_at: int) -> None:
    await publish(redis, f"user:{user_id}", "settings:updated", {"updated_at": updated_at})
