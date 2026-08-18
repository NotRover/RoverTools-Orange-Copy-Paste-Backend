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
import uuid
from datetime import UTC, datetime, timedelta
from typing import Awaitable, cast

from redis.asyncio import Redis
from sqlalchemy import and_, exists, select, text, update

from src import realtime as rt
from src.blobs import s3
from src.blobs.models import Blob
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import AsyncSessionLocal, engine
from src.spaces.models import SpaceMembership
from src.sync.models import SyncEntry
from src.redis_client import get_redis_pool

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_KEY = 0x0C11B0A5  # arbitrary stable app-wide key
_PRESENCE_SWEEP_INTERVAL = 60  # seconds
_BLOB_CLEANUP_INTERVAL = 3600  # seconds (hourly)
_LOCK_RETRY_INTERVAL = 30  # seconds between attempts to become the leader
_ORPHAN_TTL_MS = 3600 * 1000  # unconfirmed blobs older than 1h are orphans
# A blob is confirmed before the entry that points at it is pushed, and that
# push can sit in a client's offline queue for days. Only sweep blobs that have
# been unreferenced for longer than any plausible queue.
_UNREFERENCED_GRACE_MS = 7 * 24 * 3600 * 1000


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
                # Release first, so anything it frees is collected on the next
                # pass rather than sitting for another hour.
                async with AsyncSessionLocal() as db:
                    await _release_unreferenced_blobs(db)
            except Exception:
                logger.exception("unreferenced blob sweep failed")
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
        evicted = False
        for did in members:
            if not await redis.exists(f"presence:{uid}:{did}"):
                await cast("Awaitable[int]", redis.srem(set_key, did))
                await redis.publish(
                    f"user:{uid}",
                    json.dumps({"event": "device:offline", "payload": {"device_id": did}}),
                )
                offline += 1
                evicted = True
        # A socket that died without a close never ran the per-user half of the
        # goodbye, so the people who can see this user were never told. The
        # device-level event above only reaches their own channel.
        if evicted and not await rt.user_is_online(redis, uid):
            await _announce_user_offline(redis, uid)
    if offline:
        logger.info("presence sweep: evicted %d stale device(s)", offline)


async def _announce_user_offline(redis: Redis, user_id: str) -> None:
    """Tell every space this user belongs to that they are gone."""
    try:
        async with AsyncSessionLocal() as db:
            space_ids = (
                await db.scalars(
                    select(SpaceMembership.space_id).where(SpaceMembership.user_id == uuid.UUID(user_id))
                )
            ).all()
    except ValueError:
        return  # key held something that is not a user id
    await rt.publish_user_presence(redis, [f"space:{sid}" for sid in space_ids], user_id, False)


async def _release_unreferenced_blobs(db: AsyncSession) -> None:
    """Stop charging quota for blobs nothing points at any more.

    The push path releases a blob when its entry is tombstoned or replaced, but
    that only covers the transitions it can see. A blob also ends up
    unreferenced when the entry push after a confirmed upload never lands, when
    an account is deleted, or through any path added later that forgets to
    release. This is the backstop that makes those cases self-healing instead
    of permanent: whatever the cause, storage the user cannot reach stops
    counting against them, and `_cleanup_orphan_blobs` removes the object.

    Deliberately blunt - it asks "does a live entry point at this?" rather than
    tracking why - because the failure it exists to prevent is the one nobody
    predicted.
    """
    cutoff = int((datetime.now(UTC) - timedelta(milliseconds=_UNREFERENCED_GRACE_MS)).timestamp() * 1000)
    unreferenced = ~exists().where(and_(SyncEntry.blob_key == Blob.key, SyncEntry.deleted_at.is_(None)))
    keys = (
        await db.scalars(
            select(Blob.key).where(Blob.confirmed.is_(True), Blob.created_at < cutoff, unreferenced)
        )
    ).all()
    if not keys:
        return
    await db.execute(update(Blob).where(Blob.key.in_(keys)).values(confirmed=False))
    await db.commit()
    logger.info("unreferenced blob sweep: released %d blob(s)", len(keys))


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
