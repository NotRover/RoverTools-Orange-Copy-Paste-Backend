from typing import Annotated

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user_id, get_redis
from src import realtime as rt
from src.sync import service
from src.sync.models import SyncEntry
from src.sync.schemas import (
    CursorUpdateRequest,
    PullResponse,
    PushRequest,
    PushResponse,
    RemovalOut,
    SyncEntryOut,
)

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/push", response_model=PushResponse)
async def push(
    body: PushRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Push local sync entries; returns accepted entries and any conflicts.

    Requires: Bearer token + X-Device-Id header.
    Emits `sync:entry` to the user channel and each entry's space channels, and
    `space:entry_removed` to any space an entry was un-shared from.
    """
    user_id, device_id = current
    accepted, conflicts, withdrawals = await service.push_entries(db, user_id, device_id, body.entries)

    # Un-share carries no fan-out of its own: the push only reaches the spaces
    # the entry still lists, and pull matches the same array, so members would
    # keep a withdrawn entry forever.
    for w in withdrawals:
        for space_id in w.space_ids:
            await rt.publish_space_entry_removed(
                redis,
                space_id,
                w.client_id,
                w.entry_type,
                user_id,
                removed_by=user_id,
                origin_device=device_id,
            )

    # Fan-out accepted entries to connected devices via WebSocket
    for acc in accepted:
        entry = await db.scalar(select(SyncEntry).where(SyncEntry.id == acc.server_id))
        if entry:
            payload = SyncEntryOut.model_validate(entry).model_dump(mode="json")
            space_ids = [str(s) for s in (entry.space_ids or [])]
            await rt.publish_sync_entry(redis, user_id, device_id, payload, space_ids)

    return PushResponse(accepted=accepted, conflicts=conflicts)


@router.get("/pull", response_model=PullResponse)
async def pull(
    after_ts: Annotated[int, Query()] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    entry_type: Annotated[str, Query()] = "all",
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Pull sync entries changed after a timestamp, with pagination cursor.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    rows, removed, next_cursor = await service.pull_entries(db, user_id, after_ts, limit, entry_type)
    entries = [SyncEntryOut.model_validate(r) for r in rows]
    removals = [RemovalOut.model_validate(r) for r in removed]
    return PullResponse(entries=entries, removals=removals, next_cursor=next_cursor)


@router.post("/cursor", status_code=204)
async def update_cursor(
    body: CursorUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Advance the calling device's sync cursor to the given server timestamp.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, device_id = current
    await service.update_cursor(db, device_id, user_id, body.last_server_ts)

