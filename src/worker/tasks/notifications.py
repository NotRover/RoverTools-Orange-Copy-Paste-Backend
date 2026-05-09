"""
Periodic task that detects devices whose Redis presence key has expired
(i.e. the WebSocket closed without a clean unregister) and broadcasts
a `device:offline` event to the owning user channel.

Design:
- On WebSocket connect  → SADD user:{uid}:devices {did}  +  SET presence:{uid}:{did} 1 EX 300
- On WebSocket ping/pong → EXPIRE presence:{uid}:{did} 300  (refreshed every 25 s)
- On WebSocket close    → SREM user:{uid}:devices {did}  +  DEL presence:{uid}:{did}
- This task runs every 60 s, SCANs all user:*:devices sets, and for each member
  checks whether the presence key still exists.  Missing key → device went offline
  without cleanup → publish device:offline and SREM from the set.
"""

import asyncio
import json
import logging

import redis as sync_redis

from src.config import settings
from src.worker.app import app

logger = logging.getLogger(__name__)

_PRESENCE_PREFIX = "presence:"
_DEVICES_PATTERN = "user:*:devices"


@app.task(name="src.worker.tasks.notifications.detect_offline_devices")
def detect_offline_devices() -> None:
    """Scan Redis for stale device presence entries and emit device:offline events."""
    r = sync_redis.from_url(settings.redis_url, decode_responses=True)
    pub = r.pubsub()  # not used for subscribe — only for publish via r.publish

    offline_count = 0
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor, match=_DEVICES_PATTERN, count=200)
        for key in keys:
            # key format: user:{uid}:devices
            parts = key.split(":")
            if len(parts) != 3:
                continue
            uid = parts[1]

            device_ids = r.smembers(key)
            for did in device_ids:
                presence_key = f"{_PRESENCE_PREFIX}{uid}:{did}"
                if not r.exists(presence_key):
                    # Presence TTL expired — device is offline
                    r.srem(key, did)
                    _publish_device_offline(r, uid, did)
                    offline_count += 1

        if cursor == 0:
            break

    if offline_count:
        logger.info("detect_offline_devices: evicted %d stale device(s)", offline_count)

    r.close()


def _publish_device_offline(r: sync_redis.Redis, user_id: str, device_id: str) -> None:
    channel = f"user:{user_id}"
    payload = json.dumps({
        "event": "device:offline",
        "payload": {"user_id": user_id, "device_id": device_id},
    })
    r.publish(channel, payload)
