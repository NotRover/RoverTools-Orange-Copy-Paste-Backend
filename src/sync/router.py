from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user_id, get_redis
from src.limiter import limiter
from src import realtime as rt
from src.sync import service
from src.sync.models import SyncEntry
from src.sync.schemas import (
    BreakdownOut,
    CursorUpdateRequest,
    PullResponse,
    PushRequest,
    PushResponse,
    RemovalOut,
    SyncEntryOut,
)

router = APIRouter(prefix="/sync", tags=["sync"])


# The client pushes one entry per request, so an offline backlog is a long run
# of requests. 300/minute is five a second: a backlog flushes at that pace, and
# the client honours `Retry-After` on a 429 and requeues what did not go, so a
# limit costs time and never data. The row caps and the per-user push lock
# bound what a burst can do inside the limit.
@router.post("/push", response_model=PushResponse)
@limiter.limit("300/minute")
async def push(
    request: Request,
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

    # Fan-out accepted entries to connected devices via WebSocket.
    #
    # The space channels get the author's whole view, not the trimmed one pull
    # gives other readers (`service.entry_view`): the author's own devices are
    # subscribed to those channels too, and the client records the spaces a row
    # carries as the entry's share list, so a trimmed copy reaching them would
    # quietly un-share it from every other space on their next edit. Trimming
    # here needs per-recipient filtering in the hub, which knows the socket's
    # user; a channel-level publish does not.
    for acc in accepted:
        entry = await db.scalar(select(SyncEntry).where(SyncEntry.id == acc.server_id))
        if entry:
            payload = SyncEntryOut.model_validate(entry).model_dump(mode="json")
            space_ids = [str(s) for s in (entry.space_ids or [])]
            await rt.publish_sync_entry(redis, user_id, device_id, payload, space_ids)

    return PushResponse(accepted=accepted, conflicts=conflicts)


@router.get("/pull", response_model=PullResponse)
@limiter.limit("120/minute")
async def pull(
    request: Request,
    after_ts: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    entry_type: Annotated[str, Query()] = "all",
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Pull sync entries changed after a timestamp, with pagination cursor.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    entries, removed, next_cursor = await service.pull_entries(db, user_id, after_ts, limit, entry_type)
    removals = [RemovalOut.model_validate(r) for r in removed]
    return PullResponse(entries=entries, removals=removals, next_cursor=next_cursor)


@router.post("/cursor", status_code=204)
@limiter.limit("120/minute")
async def update_cursor(
    request: Request,
    body: CursorUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Advance the calling device's sync cursor to the given server timestamp.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, device_id = current
    await service.update_cursor(db, device_id, user_id, body.last_server_ts)


@router.get("/breakdown", response_model=BreakdownOut)
async def breakdown(
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Live-row counts for the account, split by kind, from one aggregate query.

    Requires: Bearer token + X-Device-Id header.
    Answers the account screen's cloud bar without paging every row down to the
    client to count there. Own rows only; tombstones excluded.
    """
    user_id, _ = current
    return await service.account_breakdown(db, user_id)

