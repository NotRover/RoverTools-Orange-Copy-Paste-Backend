import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Awaitable, cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import select

from src.auth.jwt import decode_access_token
from src.database import AsyncSessionLocal
from src.groups.models import GroupMembership
from src.realtime import hub
from src.redis_client import get_redis_pool

logger = logging.getLogger(__name__)
router = APIRouter(tags=["realtime"])

_PING_INTERVAL = 25  # seconds between server pings
_PRESENCE_TTL = 300  # seconds


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    # 1. Authenticate
    try:
        payload = decode_access_token(token)
    except Exception:
        await websocket.close(code=4001)
        return

    user_id: str = payload.get("sub", "")
    device_id: str = payload.get("did", "")
    if not user_id or not device_id:
        await websocket.close(code=4001)
        return

    redis: Redis = await get_redis_pool()  # type: ignore[assignment]

    # Check token revocation
    jti = payload.get("jti")
    if jti and await redis.exists(f"revoked:{jti}"):
        await websocket.close(code=4001)
        return

    await websocket.accept()

    # 2. Build channel list: user channel + all group channels
    channels = [f"user:{user_id}"]
    async with AsyncSessionLocal() as db:
        memberships = await db.scalars(select(GroupMembership).where(GroupMembership.user_id == uuid.UUID(user_id)))
        for m in memberships.all():
            channels.append(f"group:{m.group_id}")

    # 3. Register in hub
    await hub.register(websocket, device_id, channels)

    # 4. Update Redis presence
    await cast(Awaitable[int], redis.sadd(f"user:{user_id}:devices", device_id))
    await redis.expire(f"user:{user_id}:devices", _PRESENCE_TTL)

    # 5. Notify other devices this device came online
    from src.realtime import pubsub as rt

    await rt.publish(redis, f"user:{user_id}", "device:online", {"device_id": device_id})

    # 6. Main receive loop
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=_PING_INTERVAL)
                msg = json.loads(raw)
                event = msg.get("event")
                if event == "pong":
                    await redis.expire(f"user:{user_id}:devices", _PRESENCE_TTL)
                elif event == "ack":
                    pass  # future: mark delivery confirmed
            except asyncio.TimeoutError:
                await websocket.send_json({"event": "ping", "payload": {"server_ts": _now_ms()}})
            except (WebSocketDisconnect, RuntimeError):
                break
    finally:
        await hub.unregister(websocket)
        await cast(Awaitable[int], redis.srem(f"user:{user_id}:devices", device_id))
        await rt.publish(redis, f"user:{user_id}", "device:offline", {"device_id": device_id})
