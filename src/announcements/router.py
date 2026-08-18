from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src import realtime as rt
from src.announcements import service
from src.announcements.schemas import (
    AnnouncementCreateRequest,
    AnnouncementCreateResponse,
    AnnouncementListResponse,
)
from src.database import get_db
from src.dependencies import get_current_user_id, get_redis

router = APIRouter(prefix="/announcements", tags=["announcements"])


@router.get("", response_model=AnnouncementListResponse)
async def list_announcements(
    since: int | None = Query(default=None, ge=0, description="Client watermark, unix ms"),
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Announcements this account has not been handed yet.

    Requires: Bearer token + X-Device-Id header. Pass the highest `created_at`
    seen so far as `since`; without it the last 30 days come back, which is what
    a device signing in for the first time wants.
    """
    user_id, _ = current
    rows = await service.list_for_user(db, user_id, since)
    return AnnouncementListResponse(announcements=[service.to_out(r) for r in rows])


async def create_announcement(
    body: AnnouncementCreateRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AnnouncementCreateResponse:
    """Post an announcement and push it to whoever is connected.

    Mounted on the admin router (`POST /internal/v1/admin/announcements`), so it
    carries the admin key rather than a user token. The socket push is the fast
    path only - the row is written first, so a user with nothing connected still
    gets it from `GET /announcements`.
    """
    try:
        row = await service.create(db, body)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await rt.publish_announcement(redis, str(row.user_id) if row.user_id else None, service.to_out(row).model_dump())
    return AnnouncementCreateResponse(
        id=str(row.id),
        delivered_to=str(row.user_id) if row.user_id else "everyone",
    )


async def delete_announcement(
    announcement_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Stop serving an announcement. Devices already told keep their copy."""
    if not await service.remove(db, announcement_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such announcement")
    return {"deleted": announcement_id}
