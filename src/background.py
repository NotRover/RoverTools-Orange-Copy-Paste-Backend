"""Background maintenance — the Celery/beat replacement.

A single asyncio loop, guarded by a Postgres advisory lock so that across N API
replicas exactly one runs it at a time. If the holding replica dies, the lock is
released with its connection and another replica takes over on its next attempt.

Two jobs:
  • presence sweep — emit `device:offline` for devices whose presence TTL key
    expired without a clean WebSocket close (backstop to the instant on-close
    signal in realtime.py).
  • orphan blob cleanup — delete unconfirmed blobs older than 1h from R2 + DB.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Awaitable, cast

from sqlalchemy import select, text

from src.blobs import s3
from src.blobs.models import Blob
from src.database import AsyncSessionLocal, engine
from src.redis_client import get_redis_pool

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_KEY = 0x0C11B0A5  # arbitrary stable app-wide key
_PRESENCE_SWEEP_INTERVAL = 60  # seconds
_BLOB_CLEANUP_INTERVAL = 3600  # seconds (hourly)
_LOCK_RETRY_INTERVAL = 30  # seconds between attempts to become the leader
_ORPHAN_TTL_MS = 3600 * 1000  # unconfirmed blobs older than 1h are orphans


async def run_maintenance(stop: asyncio.Event) -> None:
    """Try to hold the advisory lock; while held, run the maintenance loop."""
    while not stop.is_set():
        try:
            async with engine.connect() as conn:
                got = await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
                if not got:
                    await _wait(stop, _LOCK_RETRY_INTERVAL)
                    continue
                logger.info("maintenance: acquired advisory lock — this replica is the leader")
                try:
                    await _leader_loop(stop)
                finally:
                    await conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("maintenance loop error; retrying")
            await _wait(stop, _LOCK_RETRY_INTERVAL)


async def _leader_loop(stop: asyncio.Event) -> None:
    elapsed_since_cleanup = _BLOB_CLEANUP_INTERVAL  # run cleanup on first tick
    while not stop.is_set():
        try:
            await _sweep_presence()
        except Exception:
            logger.exception("presence sweep failed")

        if elapsed_since_cleanup >= _BLOB_CLEANUP_INTERVAL:
            try:
                await _cleanup_orphan_blobs()
            except Exception:
                logger.exception("orphan blob cleanup failed")
            elapsed_since_cleanup = 0

        await _wait(stop, _PRESENCE_SWEEP_INTERVAL)
        elapsed_since_cleanup += _PRESENCE_SWEEP_INTERVAL


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep up to `seconds`, waking early if stop is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _sweep_presence() -> None:
    """Evict devices whose presence key expired and announce them offline."""
    redis = await get_redis_pool()
    offline = 0
    async for set_key in redis.scan_iter(match="user:*:devices", count=200):
        parts = set_key.split(":")
        if len(parts) != 3:
            continue
        uid = parts[1]
        members: set[str] = await cast("Awaitable[set[str]]", redis.smembers(set_key))
        for did in members:
            if not await redis.exists(f"presence:{uid}:{did}"):
                await cast("Awaitable[int]", redis.srem(set_key, did))
                await redis.publish(
                    f"user:{uid}",
                    json.dumps({"event": "device:offline", "payload": {"device_id": did}}),
                )
                offline += 1
    if offline:
        logger.info("presence sweep: evicted %d stale device(s)", offline)


async def _cleanup_orphan_blobs() -> None:
    cutoff = int((datetime.now(UTC) - timedelta(milliseconds=_ORPHAN_TTL_MS)).timestamp() * 1000)
    deleted = 0
    async with AsyncSessionLocal() as db:
        orphans = await db.scalars(select(Blob).where(Blob.confirmed.is_(False), Blob.created_at < cutoff))
        for blob in orphans.all():
            try:
                s3.delete_object(blob.key)
            except Exception:
                logger.exception("failed to delete R2 object %s", blob.key)
                continue
            await db.delete(blob)
            deleted += 1
        await db.commit()
    if deleted:
        logger.info("orphan blob cleanup: deleted %d blob(s)", deleted)
