import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user_id, get_redis
from src.realtime import pubsub as rt
from src.sync import service
from src.sync.models import SyncEntry
from src.sync.schemas import (
    CursorUpdateRequest,
    PullResponse,
    PushRequest,
    PushResponse,
    SyncEntryOut,
    SyncStatusResponse,
)

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/push", response_model=PushResponse)
async def push(
    body: PushRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, device_id = current
    accepted, conflicts = await service.push_entries(db, user_id, device_id, body.entries)

    # Fan-out accepted entries to connected devices via WebSocket
    for acc in accepted:
        entry = await db.scalar(select(SyncEntry).where(SyncEntry.id == acc.server_id))
        if entry:
            payload = SyncEntryOut.model_validate(entry).model_dump(mode="json")
            group_ids = [str(g) for g in (entry.group_ids or [])]
            await rt.publish_sync_entry(redis, user_id, device_id, payload, group_ids)

    return PushResponse(accepted=accepted, conflicts=conflicts)


@router.get("/pull", response_model=PullResponse)
async def pull(
    after_ts: Annotated[int, Query()] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    entry_type: Annotated[str, Query()] = "all",
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    rows, next_cursor = await service.pull_entries(db, user_id, after_ts, limit, entry_type)
    entries = [SyncEntryOut.model_validate(r) for r in rows]
    return PullResponse(entries=entries, next_cursor=next_cursor)


@router.post("/cursor", status_code=204)
async def update_cursor(
    body: CursorUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, device_id = current
    await service.update_cursor(db, device_id, user_id, body.last_server_ts)


@router.get("/status", response_model=SyncStatusResponse)
async def sync_status(
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, device_id = current
    last_ts = await service.get_cursor(db, device_id)
    return SyncStatusResponse(device_id=uuid.UUID(device_id), last_server_ts=last_ts)
